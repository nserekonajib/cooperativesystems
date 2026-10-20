import requests


def send_sms(phone_number, message):
    payload = {
        "api_key": "fV12adIaOpk4DDL0QY56D5Km1iBx7QFD576R153Dwghgz9dy6qgZiet5O2ugD8PF",
        "phone": phone_number,
        "message": message,
    }
    response = requests.post("https://yoolasms.com/api/v1/send", json=payload)
    return response.json()