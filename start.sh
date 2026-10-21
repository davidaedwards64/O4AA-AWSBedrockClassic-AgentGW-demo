#!/bin/bash
# start.sh - sets environment variables and starts the Streamlit demo app

echo "Setting up environment variables..."

# --- 1. Okta Configuration ---
export OKTA_DOMAIN="<your-org>.okta.com"        # your Okta org domain (no https://)
export OKTA_AUTH_SERVER_ID="<custom-authz-server-id>"   # the custom authorization server the Agent Gateway trusts
export OKTA_CLIENT_ID="<ai-agent-client-id>"   # the AI Agent's client ID (wlp...), not a separate native app
# Private JWK for the agent's linked app (private_key_jwt client authentication)
export OKTA_PRIVATE_KEY_FILE="./agent_key.json"

# --- 2. AWS Bedrock Configuration ---
export AWS_DEFAULT_REGION="<aws-region>"
export BEDROCK_AGENT_ID="<bedrock-agent-id>"
export BEDROCK_AGENT_ALIAS_ID="<bedrock-agent-alias-id>"

# --- 3. Script Execution ---
echo "Activating Python virtual environment..."
source .venv/bin/activate

echo "Cleaning up any old running processes on port 8501 and 8585..."
lsof -ti :8501,8585 | xargs kill -9 2>/dev/null || true

echo "Starting Streamlit app in the background..."
nohup streamlit run app.py --server.port 8501 > streamlit.log 2>&1 &
PID=$!
echo $PID > streamlit.pid
echo "Streamlit server started with PID: $PID. Logs are in streamlit.log."
echo "Your browser should open to http://localhost:8501 shortly."
echo "Use ./stop.sh to terminate the server."
echo "----------------------------------------------------"

