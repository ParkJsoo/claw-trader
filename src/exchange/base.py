from abc import ABC, abstractmethod
from domain.models import PlaceOrderRequest, PlaceOrderResult, AccountSnapshot

class ExchangeClient(ABC):

    @abstractmethod
    def ping(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def place_order(self, request: PlaceOrderRequest) -> PlaceOrderResult:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_account_snapshot(self) -> AccountSnapshot:
        raise NotImplementedError
