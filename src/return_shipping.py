"""Provider di spedizione reso. Nel prototipo è disponibile solo il mock."""

import os
import uuid
from abc import ABC, abstractmethod


class ReturnShippingProvider(ABC):
    name: str

    @abstractmethod
    def create_return(self, return_case: dict) -> dict:
        """Crea un reso e restituisce identificativo, tracking ed etichetta."""

    def get_return_status(self, return_id: str) -> dict:
        return {"return_id": return_id, "status": "waiting_for_return"}

    def cancel_return(self, return_id: str) -> dict:
        return {"return_id": return_id, "status": "cancelled"}


class MockReturnShippingProvider(ReturnShippingProvider):
    name = "mock"

    def create_return(self, return_case: dict) -> dict:
        token = uuid.uuid4().hex[:10].upper()
        return {
            "provider": self.name,
            "return_id": f"MOCK-RET-{token}",
            "tracking_number": f"MOCK{token}",
            "label_url": f"mock://return-labels/{return_case['id']}.pdf",
            "status": "created",
        }


def get_provider() -> ReturnShippingProvider:
    provider = os.getenv("RETURN_SHIPPING_PROVIDER", "mock").strip().lower()
    if provider != "mock":
        raise RuntimeError(
            "Nel prototipo è abilitato solo RETURN_SHIPPING_PROVIDER=mock."
        )
    return MockReturnShippingProvider()

