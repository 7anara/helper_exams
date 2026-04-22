import requests

res = requests.post(
    "https://api.dify.ai/v1/chat-messages",
    json={
        "inputs": {},
        "query": "Саламбы?",
        "response_mode": "blocking",
        "conversation_id": "",
        "user": "test"
    },
    headers={
        "Authorization": "Bearer app-HELigDeYtAHAl13XadJTmbAx",
        "Content-Type": "application/json"
    },
    timeout=30
)
print(res.status_code)
print(res.text[:500])