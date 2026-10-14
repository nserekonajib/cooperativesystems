import os
import uuid
import requests
from typing import Dict, Any, Optional


# ============================================================
# PAWAPAY CONFIGURATION
# ============================================================

PAWAPAY_TOKEN = "eyJraWQiOiIxIiwiYWxnIjoiRVMyNTYifQ.eyJ0dCI6IkFBVCIsInN1YiI6IjMwNzkiLCJtYXYiOiIxIiwiZXhwIjoyMTA2MDMwMTA1LCJpYXQiOjE3OTA0MTA5MDUsInBtIjoiREFGLFBBRiIsImp0aSI6IjZkNWVjNTdmLWIwZDgtNDRlNC1iNzhiLTM2ODU3MjJiZTM5ZiJ9.RUHjTQg9eOXZEndftm-CZ6vtlBxc0PLRihklsOTXOd2g3QSpf51MqhOc92Hn4lMs-Osi5wL2YOuK4vxcs4TAuw"


# For production use:
PAWAPAY_BASE_URL = "https://api.pawapay.io"


# ============================================================
# 1. AIRTEL UGANDA DEPOSIT
# ============================================================

def deposit_airtel_ug(
    phone_number: str,
    amount: str | int | float,
    api_token: Optional[str] = None,
    deposit_id: Optional[str] = None,
    client_reference_id: Optional[str] = None,
    customer_message: Optional[str] = None,
    metadata: Optional[list] = None
) -> Dict[str, Any]:
    """
    Initiate an Airtel Uganda mobile-money deposit.

    Args:
        phone_number: Customer's phone number, e.g. +256748675870
        amount: Amount in UGX
        api_token: pawaPay API token
        deposit_id: Optional custom UUID
        client_reference_id: Optional merchant reference
        customer_message: Optional message to customer
        metadata: Optional pawaPay metadata

    Returns:
        pawaPay API response as dictionary.
    """

    token = api_token or PAWAPAY_TOKEN

    if not token:
        raise ValueError("pawaPay API token is not configured.")

    # Generate deposit ID if one was not supplied
    deposit_id = deposit_id or str(uuid.uuid4())

    payload = {
        "depositId": deposit_id,
        "amount": str(amount),
        "currency": "UGX",
        "payer": {
            "type": "MMO",
            "accountDetails": {
                "phoneNumber": phone_number,
                "provider": "AIRTEL_OAPI_UGA"
            }
        }
    }

    if client_reference_id:
        payload["clientReferenceId"] = client_reference_id

    if customer_message:
        payload["customerMessage"] = customer_message

    if metadata:
        payload["metadata"] = metadata

    response = requests.post(
        f"{PAWAPAY_BASE_URL}/v2/deposits",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        timeout=30
    )

    try:
        result = response.json()
    except ValueError:
        result = {
            "error": response.text
        }

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Airtel Uganda deposit failed "
            f"(HTTP {response.status_code}): {result}"
        )

    return result


# ============================================================
# 2. MTN UGANDA DEPOSIT
# ============================================================

def deposit_mtn_ug(
    phone_number: str,
    amount: str | int | float,
    api_token: Optional[str] = None,
    deposit_id: Optional[str] = None,
    client_reference_id: Optional[str] = None,
    customer_message: Optional[str] = None,
    metadata: Optional[list] = None
) -> Dict[str, Any]:
    """
    Initiate an MTN Uganda mobile-money deposit.

    Args:
        phone_number: Customer's phone number, e.g. +256781234567
        amount: Amount in UGX
        api_token: pawaPay API token
        deposit_id: Optional custom UUID
        client_reference_id: Optional merchant reference
        customer_message: Optional message to customer
        metadata: Optional pawaPay metadata

    Returns:
        pawaPay API response as dictionary.
    """

    token = api_token or PAWAPAY_TOKEN

    if not token:
        raise ValueError("pawaPay API token is not configured.")

    deposit_id = deposit_id or str(uuid.uuid4())

    payload = {
        "depositId": deposit_id,
        "amount": str(amount),
        "currency": "UGX",
        "payer": {
            "type": "MMO",
            "accountDetails": {
                "phoneNumber": phone_number,
                "provider": "MTN_MOMO_UGA"
            }
        }
    }

    if client_reference_id:
        payload["clientReferenceId"] = client_reference_id

    if customer_message:
        payload["customerMessage"] = customer_message

    if metadata:
        payload["metadata"] = metadata

    response = requests.post(
        f"{PAWAPAY_BASE_URL}/v2/deposits",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        timeout=30
    )

    try:
        result = response.json()
    except ValueError:
        result = {
            "error": response.text
        }

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"MTN Uganda deposit failed "
            f"(HTTP {response.status_code}): {result}"
        )

    return result


# ============================================================
# 3. VERIFY DEPOSIT
# ============================================================

def verify_deposit(
    deposit_id: str,
    api_token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Verify/check the current status of a pawaPay deposit.

    Args:
        deposit_id: Deposit ID returned when initiating payment.
        api_token: pawaPay API token.

    Returns:
        Simplified deposit status information.
    """

    token = api_token or PAWAPAY_TOKEN

    if not token:
        raise ValueError("pawaPay API token is not configured.")

    response = requests.get(
        f"{PAWAPAY_BASE_URL}/v2/deposits/{deposit_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        timeout=30
    )

    if response.status_code == 404:
        return {
            "found": False,
            "deposit_id": deposit_id,
            "status": "NOT_FOUND"
        }

    try:
        result = response.json()
    except ValueError:
        result = {
            "error": response.text
        }

    if response.status_code != 200:
        raise RuntimeError(
            f"Deposit verification failed "
            f"(HTTP {response.status_code}): {result}"
        )

    # pawaPay may return the deposit directly or inside "data"
    data = result.get("data", result)

    status = data.get("status")

    return {
        "found": True,
        "deposit_id": data.get("depositId", deposit_id),
        "status": status,
        "completed": status == "COMPLETED",
        "failed": status == "FAILED",
        "pending": status not in ("COMPLETED", "FAILED"),
        "amount": data.get("amount"),
        "currency": data.get("currency"),
        "provider_transaction_id": data.get(
            "providerTransactionId"
        ),
        "failure_reason": data.get("failureReason"),
        "created": data.get("created"),
        "raw": result
    }
    
    
# number = 256731218475
# amount = 1000

# pay = deposit_airtel_ug(number, amount, PAWAPAY_TOKEN )
# print(pay)
# verify = verify_deposit(pay['depositId'], PAWAPAY_TOKEN)
# print(verify)
