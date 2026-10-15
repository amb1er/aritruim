"""Entropy HIP-3 perpetual markets on Hyperliquid HyperCore.

Entropy uses Hyperliquid's public API with HIP-3 dex id ``io`` and wire symbols
such as ``io:ASSET``. The rest of the application sees plain symbols so Entropy
markets can be compared with matching instruments on other venues.
"""

import structlog

from src.adapters.hyperliquid import HyperliquidAdapter
from src.config import Settings
from src.models import FeeRate


class EntropyAdapter(HyperliquidAdapter):
    def __init__(
        self,
        settings: Settings,
        instance_index: int | None = None,
        transport_url: str | None = None,
    ) -> None:
        resolved_index = 0 if instance_index is None else instance_index
        accounts = settings.resolved_entropy_accounts()
        if resolved_index >= len(accounts):
            raise IndexError(f"Entropy account index {resolved_index} is not configured")
        account = accounts[resolved_index]
        super().__init__(settings, 0, transport_url or account.transport_url)
        self._instance_index = resolved_index
        self._wallet_address = account.address
        self._private_key = account.credential
        self._log = structlog.get_logger(__name__).bind(instance_index=resolved_index)
        self._adapter_name = (
            "entropy" if instance_index is None else f"entropy_account_{instance_index}"
        )
        self._dex = settings.entropy_dex

    async def connect(self) -> None:
        await super().connect()
        try:
            data = await self._post({"type": "perpDexs"})
            available = {
                item.get("name") for item in data if isinstance(item, dict) and item.get("name")
            }
            if self._dex not in available:
                raise RuntimeError(
                    f"Entropy HIP-3 dex {self._dex!r} not found; "
                    f"available dexes: {sorted(available)}"
                )
        except BaseException:
            await super().disconnect()
            raise

    async def get_fee_rate(self, symbol: str) -> FeeRate:
        """Return explicit configurable HIP-3 fee assumptions."""
        return FeeRate(
            exchange=self.name,
            symbol=symbol,
            maker=self._settings.entropy_maker_fee,
            taker=self._settings.entropy_taker_fee,
        )
