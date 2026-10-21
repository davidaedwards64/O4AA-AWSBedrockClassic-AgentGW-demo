import os
import json
import base64
import hashlib
import requests
import secrets
import threading
import webbrowser
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import socketserver
import time
import uuid
import jwt  # PyJWT
import platform
import subprocess
from datetime import datetime, timezone

import streamlit as st
import boto3

# --- Configuration ---
OKTA_DOMAIN = os.environ.get("OKTA_DOMAIN")
OKTA_AUTH_SERVER_ID = os.environ.get("OKTA_AUTH_SERVER_ID")
OKTA_CLIENT_ID = os.environ.get("OKTA_CLIENT_ID")
OKTA_PRIVATE_KEY_FILE = os.environ.get("OKTA_PRIVATE_KEY_FILE")
BEDROCK_AGENT_ID = os.environ.get("BEDROCK_AGENT_ID")
BEDROCK_AGENT_ALIAS_ID = os.environ.get("BEDROCK_AGENT_ALIAS_ID")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")

# --- Use Port 8585 for the callback to avoid conflicts ---
REDIRECT_URI = "http://127.0.0.1:8585/callback"
AUTH_CODE = None # Global variable to hold the auth code from Okta

class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True

class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global AUTH_CODE
        query_components = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if 'code' in query_components:
            AUTH_CODE = query_components['code'][0]
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Authentication successful!</h1><p>You can close this browser tab.</p></body></html>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Error: Authorization code not found.</h1></body></html>")
        
        threading.Thread(target=self.server.shutdown).start()

def build_client_assertion(token_url):
    """Signed JWT proving this client's identity (token_endpoint_auth_method = private_key_jwt)."""
    with open(OKTA_PRIVATE_KEY_FILE) as f:
        key = json.load(f)
    now = int(time.time())
    claims = {
        "iss": OKTA_CLIENT_ID, "sub": OKTA_CLIENT_ID, "aud": token_url,
        "iat": now, "exp": now + 300, "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, jwt.PyJWK(key).key, algorithm="RS256", headers={"kid": key["kid"]})

def perform_pkce_login():
    global AUTH_CODE
    AUTH_CODE = None  # don't reuse a code from an earlier attempt
    st.session_state.login_error = None

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    
    auth_url = (
        f"https://{OKTA_DOMAIN}/oauth2/{OKTA_AUTH_SERVER_ID}/v1/authorize?"
        f"client_id={OKTA_CLIENT_ID}&"
        f"response_type=code&"
        f"scope=openid%20profile%20email%20offline_access&"
        f"redirect_uri={urllib.parse.quote(REDIRECT_URI)}&"
        f"state=streamlit_demo_state&"
        f"code_challenge={challenge}&"
        f"code_challenge_method=S256"
    )
    
    server = ReusableTCPServer(("127.0.0.1", 8585), CallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()

    try:
        if platform.system() == 'Darwin': # macOS
            subprocess.run(['open', '-na', 'Google Chrome', '--args', '--incognito', auth_url])
        elif platform.system() == 'Windows':
            subprocess.run(['start', 'chrome', '/incognito', auth_url], shell=True)
        else: # Fallback for other OS
            webbrowser.open(auth_url)
    except FileNotFoundError:
        webbrowser.open(auth_url)

    server_thread.join(timeout=60)
    server.server_close()

    if not AUTH_CODE:
        st.session_state.login_error = "Authentication timed out or failed. Please try again."
        return None, None
        
    try:
        token_url = f"https://{OKTA_DOMAIN}/oauth2/{OKTA_AUTH_SERVER_ID}/v1/token"
        data = {
            "grant_type": "authorization_code",
            "client_id": OKTA_CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code": AUTH_CODE,
            "code_verifier": verifier,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": build_client_assertion(token_url),
        }
        resp = requests.post(token_url, data=data)
        resp.raise_for_status()
        tokens = resp.json()

        user_info_resp = requests.get(
            f"https://{OKTA_DOMAIN}/oauth2/{OKTA_AUTH_SERVER_ID}/v1/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        user_info_resp.raise_for_status()
        
        st.session_state.token = tokens['access_token']
        st.session_state.tokens = {**tokens, "issued_at": int(time.time())}
        st.session_state.user_info = user_info_resp.json()
        st.session_state.messages = []
        st.session_state.agent_session_id = secrets.token_hex(16)

        return st.session_state.token, st.session_state.user_info
    except requests.exceptions.RequestException as e:
        detail = e.response.text if e.response is not None else str(e)
        print(f"[ERROR] Okta token/userinfo request failed: {detail}", flush=True)
        # st.rerun() follows the login call and would wipe a direct st.error, so keep it in session state
        st.session_state.login_error = f"Error during token exchange: {detail}"
        return None, None

# --- Function to go invoke the Bedrock agent ---
def invoke_bedrock_agent(access_token, prompt, session_id):
    """Returns the agent's reply text; raises on failure so the caller can show it in the chat."""
    client = boto3.client('bedrock-agent-runtime', region_name=AWS_REGION)

    # These are the attributes we want to pass to the Lambda
    session_state = {
        'sessionAttributes': {
            'accessToken': access_token,
            'clientId': OKTA_CLIENT_ID
        }
    }

    response = client.invoke_agent(
        agentId=BEDROCK_AGENT_ID,
        agentAliasId=BEDROCK_AGENT_ALIAS_ID,
        sessionId=session_id,  # reused across turns so the agent keeps conversation context
        inputText=prompt,
        sessionState=session_state,
        enableTrace=False
    )

    final_completion = ""
    for event in response['completion']:
        if 'chunk' in event:
            final_completion += event['chunk']['bytes'].decode('utf-8')
    return final_completion


# --- Token display helpers ---
def fmt_ts(value):
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

def fmt_delta(seconds):
    seconds = int(abs(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

def expiry_text(exp):
    remaining = exp - time.time()
    return f"🟢 expires in {fmt_delta(remaining)}" if remaining > 0 else f"🔴 expired {fmt_delta(remaining)} ago"

SUMMARY_CLAIMS = ["iss", "sub", "aud", "cid", "azp", "scp", "scope", "act", "amr", "auth_time", "iat", "nbf", "exp", "jti"]
TIME_CLAIMS = {"iat", "nbf", "exp", "auth_time"}

def render_token(label, token):
    st.markdown(f"**{label}**")
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        st.caption(f"Opaque token (not a JWT), {len(token)} characters.")
        return
    if "exp" in claims:
        st.caption(expiry_text(claims["exp"]))
    rows = []
    for k in SUMMARY_CLAIMS:
        if k in claims:
            v = claims[k]
            if k in TIME_CLAIMS:
                v = fmt_ts(v)
            elif not isinstance(v, str):
                v = json.dumps(v)
            rows.append({"Claim": k, "Value": v})
    st.table(rows)

def render_token_panel():
    st.subheader("Active tokens")
    tokens = st.session_state.get("tokens") or {}
    if tokens.get("id_token"):
        render_token("ID token", tokens["id_token"])
    if tokens.get("access_token"):
        render_token("Access token", tokens["access_token"])
    meta = {k: tokens[k] for k in ("token_type", "scope", "expires_in") if k in tokens}
    if "issued_at" in tokens:
        meta["issued_at"] = fmt_ts(tokens["issued_at"])
    if meta:
        st.markdown("**Token response**")
        st.table([{"Field": k, "Value": str(v)} for k, v in meta.items()])


# --- Streamlit UI ---
st.set_page_config(layout="wide")
st.title("Okta + AWS Bedrock Agent Demo")

if 'token' not in st.session_state:
    st.session_state.token = None
    st.session_state.user_info = None
if st.session_state.get('login_error'):
    st.error(st.session_state.login_error)

with st.sidebar:
    if st.session_state.token:
        st.success(f"Authenticated as: **{st.session_state.user_info.get('name')}**")
        show_tokens = st.toggle("Show active tokens", value=True)
        if st.button("New chat"):
            st.session_state.messages = []
            st.session_state.agent_session_id = secrets.token_hex(16)
            st.rerun()
        if st.button("Sign Out"):
            for key in ("token", "tokens", "user_info", "messages", "agent_session_id"):
                st.session_state[key] = None
            st.rerun()
    else:
        st.info("You are not signed in.")
        if st.button("🔑 Sign in with Okta"):
            with st.spinner("Waiting for Okta login in browser..."):
                perform_pkce_login()
            st.rerun()

def queue_prompt():
    """Form callback: remember the submitted text so the chat column can process it in order."""
    text = (st.session_state.get("prompt_text") or "").strip()
    if text:
        st.session_state.pending_prompt = text

if st.session_state.token:
    if show_tokens:
        chat_col, token_col = st.columns([3, 2], gap="large")
    else:
        chat_col, token_col = st.container(), None

    with chat_col:
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        prompt = st.session_state.pop("pending_prompt", None)
        if prompt:
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)
            with st.chat_message("assistant"):
                with st.spinner("Invoking Bedrock Agent via Okta Gateway..."):
                    try:
                        reply = invoke_bedrock_agent(
                            st.session_state.token, prompt, st.session_state.agent_session_id
                        ) or "(The agent returned an empty response.)"
                    except Exception as e:
                        reply = f"⚠️ An error occurred while invoking the agent: {e}"
                st.markdown(reply)
            st.session_state.messages.append({"role": "assistant", "content": reply})

        # The prompt box sits after the latest response, not pinned to the bottom of the page.
        with st.form("prompt_form", clear_on_submit=True, border=False):
            st.text_input(
                "Prompt",
                key="prompt_text",
                label_visibility="collapsed",
                placeholder="Ask about your GitHub repositories, e.g. 'List the 3 most recently updated repositories in my account.'",
            )
            st.form_submit_button("Send", on_click=queue_prompt)

    if token_col is not None:
        with token_col:
            render_token_panel()
else:
    st.warning("Please sign in using the button in the sidebar to use the agent.")


