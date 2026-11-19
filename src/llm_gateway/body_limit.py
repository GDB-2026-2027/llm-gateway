import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    """Reject oversized request bodies before FASTAPI reads them."""

    def __init__(
        self,
        app: ASGIApp,
        max_body_bytes: int,
        max_embeddings_body_bytes: int | None = None,
    ):
        if type(max_body_bytes) is not int or max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be a positive integer.")

        if max_embeddings_body_bytes is not None and (
            type(max_embeddings_body_bytes) is not int or max_embeddings_body_bytes <= 0
        ):
            raise ValueError("max_embeddings_body_bytes must be a positive integer.")

        self.max_embeddings_body_bytes = (
            max_embeddings_body_bytes
            if max_embeddings_body_bytes is not None
            else max_body_bytes
        )

        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return

        limit = (
            self.max_embeddings_body_bytes
            if scope.get("path", "") == "/v1/embeddings"
            else self.max_body_bytes
        )

        # Reject a declared oversized body before reading it.
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    content_length = int(value)
                except ValueError:
                    continue  # The streamed-byte check below still enforces the limit.

                if content_length > limit:
                    await self._reject(send)
                    return

        # Count actual bytes, including requests without a valid length header.
        body = bytearray()

        while True:
            message = await receive()

            if message["type"] == "http.disconnect":
                return

            if message["type"] != "http.request":
                continue

            chunk = message.get("body", b"")

            if len(body) + len(chunk) > limit:
                await self._reject(send)
                return

            body.extend(chunk)

            if not message.get("more_body", False):
                break

        # Replay the accepted body once so FastAPI can parse it normally.
        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed

            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(send: Send) -> None:
        """Return HTTP 413 without invoking the endpoint."""

        body = json.dumps({"detail": "Request body too large"}).encode("utf-8")

        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )

        await send(
            {
                "type": "http.response.body",
                "body": body,
            }
        )
