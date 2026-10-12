"""Entropy HIP-3 perpetual markets on Hyperliquid HyperCore.

Entropy uses Hyperliquid's public API with HIP-3 dex id ``io`` and wire symbols
such as ``io:ASSET``. The rest of the application sees plain symbols so Entropy
markets can be compared with matching instruments on other venues.
"""

from src.adapters.hyperliquid import HyperliquidAdapter
from src.config import Settings
from src.models import FeeRate


class EntropyAdapter(HyperliquidAdapter):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._adapter_name = "entropy"
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
