"""WABridge — async HTTP client to wa-bridge Node.js sidecar."""

import logging

import aiohttp

log = logging.getLogger("nexus")


class WABridge:
    """Async client for a single wa-bridge instance."""

    def __init__(self, name: str, base_url: str):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_status(self) -> dict:
        """GET /status — connection health."""
        try:
            session = await self._get_session()
            async with session.get(f"{self.base_url}/status") as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"status": "error", "http_status": resp.status}
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}

    async def send_text(self, jid: str, text: str) -> dict:
        """POST /send — send a text message."""
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/send",
            json={"jid": jid, "text": text},
        ) as resp:
            return await resp.json()

    async def send_voice(self, jid: str, audio_b64: str) -> dict:
        """POST /send-voice — send a voice note (base64 OGG Opus)."""
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/send-voice",
            json={"jid": jid, "audio_b64": audio_b64},
        ) as resp:
            return await resp.json()

    async def get_contacts(self) -> list[dict]:
        """GET /contacts — list recent chats."""
        try:
            session = await self._get_session()
            async with session.get(f"{self.base_url}/contacts") as resp:
                data = await resp.json()
                return data.get("contacts", [])
        except Exception:
            return []
