import os

API_KEY_302 = os.getenv("API_KEY_302")

headers_302 = {
    'Authorization': f'Bearer {API_KEY_302}'
}

headers_json_302 = {
    'Authorization': f'Bearer {API_KEY_302}',
    'Content-Type': 'application/json'
}