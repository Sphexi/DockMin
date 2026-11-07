"""A fake bucket that also accepts multipart uploads, with the means to fail each operation."""

import asyncio
import hashlib
import itertools
import re
from urllib.parse import parse_qs

import httpx

from tests.object_storage.wire_support import XML, check_signature
from tests.subsystems.syncer_support import Bucket, Step

PART_ETAG = re.compile(r"<PartNumber>(\d+)</PartNumber><ETag>\"([0-9a-f]+)\"</ETag>")


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def on_request(method: str, marker: str, count: int, outcome: httpx.Response | Exception) -> Step:
    """Answer the first `count` requests of a method whose query holds `marker` with `outcome`."""
    left = [count]

    def step(request: httpx.Request) -> httpx.Response | Exception | None:
        if request.method == method and marker in request.url.query.decode() and left[0] > 0:
            left[0] -= 1
            return outcome
        return None

    return step


def error_xml(status: int, code: str) -> httpx.Response:
    body = f'<?xml version="1.0"?><Error><Code>{code}</Code><Message>m</Message></Error>'
    return httpx.Response(status, content=body.encode(), headers=XML)


class MultipartBucket(Bucket):
    """Keeps parts by upload, assembles them on complete and records every operation."""

    def __init__(self) -> None:
        super().__init__()
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.upload_keys: dict[str, str] = {}
        self.operations: list[str] = []
        self.aborted: list[str] = []
        self.part_md5_headers: list[str | None] = []
        self.abort_hold: asyncio.Event | None = None
        self.abort_entered = asyncio.Event()
        self._ids = itertools.count(1)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode(), keep_blank_values=True)
        operation = self._operation(request, query)
        if operation is None:
            return await super().handler(request)
        body = await request.aread()
        try:
            check_signature(request, body)
        except AssertionError as failure:
            self.problems.append(str(failure))
        self.operations.append(operation)
        self.log.append(f"{request.method} {request.url.path}")
        for step in self.script:
            outcome = step(request)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is not None:
                return outcome
        key = request.url.path.split("/", 2)[2]
        match request.method, query:
            case "POST", {"uploads": _}:
                return self._create(key)
            case "PUT", _:
                return await self._part(request, body, query)
            case "POST", _:
                return self._complete(key, body, query["uploadId"][0])
            case _:
                return await self._abort(query["uploadId"][0])

    @staticmethod
    def _operation(request: httpx.Request, query: dict[str, list[str]]) -> str | None:
        match request.method, query:
            case "POST", {"uploads": _}:
                return "create"
            case "PUT", {"partNumber": [number], "uploadId": _}:
                return f"part {number}"
            case "POST", {"uploadId": _}:
                return "complete"
            case "DELETE", {"uploadId": _}:
                return "abort"
        return None

    def _create(self, key: str) -> httpx.Response:
        upload_id = f"upload-{next(self._ids)}"
        self.uploads[upload_id] = {}
        self.upload_keys[upload_id] = key
        body = (
            '<?xml version="1.0"?><InitiateMultipartUploadResult xmlns="x">'
            f"<Bucket>b</Bucket><Key>{key}</Key><UploadId>{upload_id}</UploadId>"
            "</InitiateMultipartUploadResult>"
        )
        return httpx.Response(200, content=body.encode(), headers=XML)

    async def _part(
        self, request: httpx.Request, body: bytes, query: dict[str, list[str]]
    ) -> httpx.Response:
        self.part_md5_headers.append(request.headers.get("content-md5"))
        if self.hold is not None:
            self.entered.set()
            await self.hold.wait()
        upload = self.uploads.get(query["uploadId"][0])
        if upload is None:
            return error_xml(404, "NoSuchUpload")
        upload[int(query["partNumber"][0])] = body
        return httpx.Response(200, headers={"ETag": f'"{md5_hex(body)}"'})

    def _complete(self, key: str, body: bytes, upload_id: str) -> httpx.Response:
        upload = self.uploads.get(upload_id)
        if upload is None:
            return error_xml(404, "NoSuchUpload")
        listed = [(int(n), tag) for n, tag in PART_ETAG.findall(body.decode())]
        if [n for n, _ in listed] != sorted(upload) or any(
            md5_hex(upload[n]) != tag for n, tag in listed
        ):
            return error_xml(400, "InvalidPart")
        data = b"".join(upload[n] for n, _ in listed)
        joined = b"".join(bytes.fromhex(tag) for _, tag in listed)
        self.objects[key] = data
        del self.uploads[upload_id]
        result = (
            '<?xml version="1.0"?><CompleteMultipartUploadResult xmlns="x">'
            f"<Location>l</Location><ETag>&quot;{md5_hex(joined)}-{len(listed)}&quot;</ETag>"
            "</CompleteMultipartUploadResult>"
        )
        return httpx.Response(200, content=result.encode(), headers=XML)

    async def _abort(self, upload_id: str) -> httpx.Response:
        if self.abort_hold is not None:
            self.abort_entered.set()
            await self.abort_hold.wait()
        if self.uploads.pop(upload_id, None) is None:
            return error_xml(404, "NoSuchUpload")
        self.aborted.append(upload_id)
        return httpx.Response(204)
