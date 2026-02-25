# type: ignore
"""
Phoenixd Lightning Backend for Cashu Nutshell

Phoenixd is the server equivalent of the Phoenix wallet.
API docs: https://phoenix.acinq.co/server/api
"""
import asyncio
from typing import AsyncGenerator, Optional

import httpx
from bolt11 import decode
from loguru import logger

from ..core.base import Amount, MeltQuote, Unit
from ..core.helpers import fee_reserve
from ..core.models import PostMeltQuoteRequest
from ..core.settings import settings
from .base import (
    InvoiceResponse,
    LightningBackend,
    PaymentQuoteResponse,
    PaymentResponse,
    PaymentResult,
    PaymentStatus,
    StatusResponse,
)


class PhoenixdWallet(LightningBackend):
    """Phoenixd Lightning Backend
    
    https://github.com/ACINQ/phoenixd
    """

    supported_units = {Unit.sat}
    unit = Unit.sat
    supports_incoming_payment_stream: bool = True
    supports_description: bool = True

    def __init__(self, unit: Unit = Unit.sat, **kwargs):
        self.assert_unit_supported(unit)
        self.unit = unit
        self.endpoint = settings.mint_phoenixd_url
        self.client = httpx.AsyncClient(
            verify=not settings.debug,
            auth=httpx.BasicAuth("", settings.mint_phoenixd_password),
            timeout=None,
        )

    async def status(self) -> StatusResponse:
        """Get node status and balance"""
        try:
            r = await self.client.get(url=f"{self.endpoint}/getbalance", timeout=15)
            r.raise_for_status()
            data: dict = r.json()
        except Exception as exc:
            return StatusResponse(
                error_message=f"Failed to connect to phoenixd at {self.endpoint}: {exc}",
                balance=Amount(self.unit, 0),
            )

        balance_sat = data.get("balanceSat", 0)
        return StatusResponse(
            error_message=None,
            balance=Amount(Unit.sat, balance_sat),
        )

    async def create_invoice(
        self,
        amount: Amount,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
        unhashed_description: Optional[bytes] = None,
    ) -> InvoiceResponse:
        """Create a Lightning invoice"""
        self.assert_unit_supported(amount.unit)

        data = {
            "amountSat": amount.to(Unit.sat).amount,
            "description": memo or "",
        }
        
        # Phoenixd supports description hash via descriptionHash parameter
        if description_hash:
            data["descriptionHash"] = description_hash.hex()

        try:
            r = await self.client.post(
                url=f"{self.endpoint}/createinvoice",
                data=data,
            )
            r.raise_for_status()
            result = r.json()
        except httpx.HTTPStatusError as e:
            return InvoiceResponse(
                ok=False,
                error_message=f"HTTP error: {e.response.status_code} - {e.response.text}",
            )
        except Exception as exc:
            return InvoiceResponse(ok=False, error_message=str(exc))

        payment_hash = result.get("paymentHash")
        bolt11 = result.get("serialized")

        if not payment_hash or not bolt11:
            return InvoiceResponse(
                ok=False,
                error_message="Invalid response from phoenixd: missing paymentHash or serialized",
            )

        return InvoiceResponse(
            ok=True,
            checking_id=payment_hash,
            payment_request=bolt11,
        )

    async def pay_invoice(
        self, quote: MeltQuote, fee_limit_msat: int
    ) -> PaymentResponse:
        """Pay a Lightning invoice"""
        try:
            r = await self.client.post(
                url=f"{self.endpoint}/payinvoice",
                data={"invoice": quote.request},
                timeout=None,
            )
            r.raise_for_status()
            data: dict = r.json()
        except httpx.HTTPStatusError as e:
            error_msg = f"HTTP error: {e.response.status_code}"
            try:
                error_data = e.response.json()
                if "message" in error_data:
                    error_msg = error_data["message"]
            except Exception:
                error_msg = e.response.text
            return PaymentResponse(
                result=PaymentResult.FAILED,
                error_message=error_msg,
            )
        except Exception as exc:
            return PaymentResponse(result=PaymentResult.FAILED, error_message=str(exc))

        # Phoenixd returns payment details on success
        payment_hash = data.get("paymentHash")
        preimage = data.get("paymentPreimage")
        fees_sat = data.get("routingFeeSat", 0)

        if not payment_hash:
            return PaymentResponse(
                result=PaymentResult.UNKNOWN,
                error_message="No paymentHash in response",
            )

        return PaymentResponse(
            result=PaymentResult.SETTLED,
            checking_id=payment_hash,
            fee=Amount(Unit.sat, fees_sat),
            preimage=preimage,
        )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        """Check status of an incoming invoice by payment hash"""
        try:
            r = await self.client.get(
                url=f"{self.endpoint}/payments/incoming",
                params={"paymentHash": checking_id},
            )
            r.raise_for_status()
            data: list = r.json()
        except Exception as e:
            return PaymentStatus(result=PaymentResult.UNKNOWN, error_message=str(e))

        # Find matching payment
        for payment in data:
            if payment.get("paymentHash") == checking_id:
                if payment.get("isPaid"):
                    return PaymentStatus(
                        result=PaymentResult.SETTLED,
                        preimage=payment.get("preimage"),
                    )
                elif payment.get("isExpired"):
                    return PaymentStatus(result=PaymentResult.FAILED)
                else:
                    return PaymentStatus(result=PaymentResult.PENDING)

        # Invoice not found - might be pending or not created yet
        return PaymentStatus(result=PaymentResult.PENDING)

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        """Check status of an outgoing payment by payment hash"""
        try:
            r = await self.client.get(
                url=f"{self.endpoint}/payments/outgoing",
                params={"paymentHash": checking_id},
            )
            r.raise_for_status()
            data: list = r.json()
        except Exception as e:
            return PaymentStatus(result=PaymentResult.UNKNOWN, error_message=str(e))

        # Find matching payment
        for payment in data:
            if payment.get("paymentHash") == checking_id:
                if payment.get("isPaid"):
                    fees_sat = payment.get("fees", 0)
                    # fees from phoenixd are in millisats for outgoing
                    if fees_sat > 1000:
                        fees_sat = fees_sat // 1000
                    return PaymentStatus(
                        result=PaymentResult.SETTLED,
                        fee=Amount(Unit.sat, fees_sat),
                        preimage=payment.get("preimage"),
                    )
                else:
                    return PaymentStatus(result=PaymentResult.PENDING)

        return PaymentStatus(result=PaymentResult.UNKNOWN)

    async def get_payment_quote(
        self, melt_quote: PostMeltQuoteRequest
    ) -> PaymentQuoteResponse:
        """Get a quote for paying an invoice"""
        invoice_obj = decode(melt_quote.request)
        assert invoice_obj.amount_msat, "invoice has no amount."
        amount_msat = int(invoice_obj.amount_msat)
        
        # Phoenixd charges ~0.4% + 4 sats for Lightning payments
        # We use a conservative estimate
        fees_msat = fee_reserve(amount_msat)
        fees = Amount(unit=Unit.msat, amount=fees_msat)
        amount = Amount(unit=Unit.msat, amount=amount_msat)
        
        return PaymentQuoteResponse(
            checking_id=invoice_obj.payment_hash,
            fee=fees.to(self.unit, round="up"),
            amount=amount.to(self.unit, round="up"),
        )

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        """Stream of paid invoice payment hashes
        
        Phoenixd supports webhooks for payment notifications.
        This implementation polls for new payments as a fallback.
        For production, configure webhooks in phoenixd.
        """
        seen_payments: set[str] = set()
        poll_interval = 5  # seconds
        
        # Initial load of existing payments to avoid re-processing
        try:
            r = await self.client.get(
                url=f"{self.endpoint}/payments/incoming",
                params={"count": 100},
            )
            r.raise_for_status()
            for payment in r.json():
                if payment.get("isPaid"):
                    seen_payments.add(payment.get("paymentHash", ""))
        except Exception as e:
            logger.warning(f"Failed to load initial payments: {e}")

        while True:
            try:
                r = await self.client.get(
                    url=f"{self.endpoint}/payments/incoming",
                    params={"count": 20},
                )
                r.raise_for_status()
                payments = r.json()
                
                for payment in payments:
                    payment_hash = payment.get("paymentHash")
                    if payment.get("isPaid") and payment_hash not in seen_payments:
                        seen_payments.add(payment_hash)
                        logger.info(f"phoenixd: payment received {payment_hash}")
                        yield payment_hash

            except Exception as e:
                logger.error(f"phoenixd: error polling payments: {e}")
            
            await asyncio.sleep(poll_interval)
