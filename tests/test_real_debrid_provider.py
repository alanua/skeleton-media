from __future__ import annotations

import pytest

from skeleton_media.cast.real_debrid_provider import (
    AccountStatus,
    DeviceCredentials,
    DeviceFlow,
    ProviderError,
    ProviderResponse,
    RealDebridProvider,
)


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, params, headers, timeout):
        self.calls.append((method, url, dict(params or {}), dict(headers), timeout))
        if not self.responses:
            raise AssertionError("unexpected transport request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


DEVICE_RESPONSE = {
    "device_code": "private-device-code-ABC",
    "user_code": "PUBLIC-USER-CODE",
    "verification_url": "https://real-debrid.com/device",
    "interval": 5,
    "expires_in": 1800,
}


def response(payload, *, status=200, headers=None):
    return ProviderResponse(status, payload, headers or {"Content-Type": "application/json"})


def provider(transport, *, token="SYNTHETIC_SECRET_NEVER_LOG", now=None):
    return RealDebridProvider(
        transport=transport,
        client_id="TEST_CLIENT_1234",
        access_token=(lambda: token),
        clock=now or (lambda: 100.0),
    )


def test_constructor_does_not_perform_io():
    fake = FakeTransport()
    provider(fake)
    assert fake.calls == []


def test_device_flow_does_not_use_bearer_and_requests_only_fixed_host():
    fake = FakeTransport(response(DEVICE_RESPONSE))
    flow = provider(fake).begin_device_flow()
    method, url, params, headers, timeout = fake.calls[0]
    assert method == "GET"
    assert url == "https://api.real-debrid.com/oauth/v2/device/code"
    assert params == {"client_id": "TEST_CLIENT_1234", "new_credentials": "yes"}
    assert "Authorization" not in headers
    assert timeout == 8
    assert flow.public_view(now=100)["expires_in"] == 1800


def test_ephemeral_codes_hidden_in_object_repr():
    flow = provider(FakeTransport(response(DEVICE_RESPONSE))).begin_device_flow()
    assert DEVICE_RESPONSE["device_code"] not in repr(flow)
    assert DEVICE_RESPONSE["user_code"] not in repr(flow)
    assert "redacted" in repr(flow).lower()


def test_device_flow_pending_does_not_disclose_error_payload():
    fake = FakeTransport(response({"error": "private provider message"}, status=403))
    p = provider(fake)
    flow = DeviceFlow("private-code", "operator-code", "https://real-debrid.com/device", 5, 500)
    assert p.poll_device_flow(flow).status == "PENDING"


def test_device_flow_expired_without_network():
    fake = FakeTransport()
    p = provider(fake)
    flow = DeviceFlow("secret", "operator", "https://real-debrid.com/device", 5, 99)
    assert p.poll_device_flow(flow).status == "EXPIRED"
    assert not fake.calls


def test_device_credentials_are_private():
    fake = FakeTransport(response({"client_id": "NEW_USER_BOUND_ID", "client_secret": "SUPER_SECRET_VALUE"}))
    flow = DeviceFlow("private-code", "operator", "https://real-debrid.com/device", 5, 500)
    result = provider(fake).poll_device_flow(flow)
    assert result.status == "AUTHORIZED"
    assert isinstance(result.credentials, DeviceCredentials)
    assert result.credentials.client_id == "NEW_USER_BOUND_ID"
    assert result.credentials.client_secret == "SUPER_SECRET_VALUE"
    assert "SUPER_SECRET_VALUE" not in repr(result)
    assert "SUPER_SECRET_VALUE" not in repr(result.credentials)
    assert fake.calls[0][2] == {"client_id": "TEST_CLIENT_1234", "code": "private-code"}


@pytest.mark.parametrize("url", [
    "http://real-debrid.com/device",
    "https://evil.invalid/device",
    "https://real-debrid.com.evil.invalid/device",
    "https://real-debrid.com/another",
    "https://user@real-debrid.com/device",
    "https://real-debrid.com:444/device",
    "https://real-debrid.com:bad/device",
    "https://real-debrid.com/device?code=private",
    "https://real-debrid.com/device#private",
])
def test_device_flow_rejects_untrusted_verification_urls(url):
    data = {**DEVICE_RESPONSE, "verification_url": url}
    with pytest.raises(ProviderError) as exc:
        provider(FakeTransport(response(data))).begin_device_flow()
    assert exc.value.reason_code == "INVALID_VERIFICATION_URL"


@pytest.mark.parametrize("field,value", [
    ("device_code", None),
    ("user_code", ""),
    ("interval", -1),
    ("expires_in", 12),
    ("interval", "not_an_integer"),
])
def test_device_flow_rejects_invalid_provider_values(field, value):
    data = {**DEVICE_RESPONSE, field: value}
    with pytest.raises(ProviderError) as exc:
        provider(FakeTransport(response(data))).begin_device_flow()
    assert exc.value.reason_code == "INVALID_RESPONSE"


def test_read_only_account_status_drops_identity():
    fake = FakeTransport(response({
        "username": "private-account-name",
        "email": "private@example.invalid",
        "type": "premium",
        "premium": 86400,
        "expiration": "2026-11-24T12:00:00.000Z",
    }))
    result = provider(fake).account_status()
    assert result == AccountStatus("premium", 86400, "2026-11-24T12:00:00.000Z")
    assert "email" not in repr(result)
    assert fake.calls[0][1].endswith("/rest/1.0/user")
    assert fake.calls[0][3]["Authorization"] == "Bearer SYNTHETIC_SECRET_NEVER_LOG"


def test_torrents_are_candidates_not_playable_sources():
    fake = FakeTransport(response([
        {"id": "t-one", "filename": "user-authorized-test-video.mkv",
         "bytes": 50000, "status": "downloaded", "links": ["https://signed.invalid/a"]},
        {"id": "t-one", "filename": "duplicate.mkv"},
        {"id": "t-two", "filename": "waiting.avi", "bytes": 42, "status": "waiting_files_selection"},
    ]))
    result = provider(fake).list_torrents()
    assert len(result) == 2
    assert result[0].verified_playable is False
    assert result[0].status == "downloaded"
    assert result[0].size_bytes == 50000
    assert "signed.invalid" not in repr(result)
    assert all(not hasattr(x, "url") and not hasattr(x, "links") for x in result)


def test_downloads_are_read_only_without_temporary_signed_links():
    fake = FakeTransport(response([
        {"id": "d-1", "filename": "owned-file.mp4",
         "filesize": 999, "link": "https://private.invalid/ref",
         "download": "https://private.invalid/signed"}
    ]))
    result = provider(fake).list_downloads()
    assert len(result) == 1
    assert result[0].kind == "download"
    assert result[0].size_bytes == 999
    assert result[0].verified_playable is False
    assert "private.invalid" not in repr(result)


def test_empty_inventory():
    assert provider(FakeTransport(response([]))).list_torrents() == ()


def test_invalid_inventory_payload():
    with pytest.raises(ProviderError, match="INVALID_RESPONSE"):
        provider(FakeTransport(response({"items": []}))).list_downloads()


def test_provider_items_require_usable_ids_and_dedupe():
    rows = [{"id": 123, "filename": "bad"},
            {"id": "../escape", "filename": "bad"},
            {"id": "ok", "filename": "one", "bytes": True},
            {"id": "ok", "filename": "two"}]
    items = provider(FakeTransport(response(rows))).list_torrents()
    assert len(items) == 1
    assert items[0].size_bytes is None


@pytest.mark.parametrize("page,limit", [(0, 1), (-1, 100), (1, 0), (1, 5001), (True, 1), (1, "50")])
def test_invalid_pagination_is_rejected_before_transport(page, limit):
    fake = FakeTransport()
    with pytest.raises(ProviderError, match="INVALID_PAGINATION"):
        provider(fake).list_torrents(page=page, limit=limit)
    assert not fake.calls


def test_inventory_request_boundaries():
    fake = FakeTransport(response([]))
    provider(fake).list_downloads(page=3, limit=5000)
    assert fake.calls[0][2] == {"page": "3", "limit": "5000"}
    assert fake.calls[0][0] == "GET"
    assert fake.calls[0][1] == "https://api.real-debrid.com/rest/1.0/downloads"


@pytest.mark.parametrize("status,expected", [
    (401, "AUTH_REQUIRED"), (403, "PERMISSION_DENIED"),
    (429, "RATE_LIMITED"), (503, "PROVIDER_UNAVAILABLE"),
    (404, "PROVIDER_REJECTED"),
])
def test_http_errors_are_redacted(status, expected):
    fake = FakeTransport(response({"error": "DO_NOT_DISPLAY_PROVIDER_ERROR"}, status=status))
    with pytest.raises(ProviderError) as exc:
        provider(fake).account_status()
    assert exc.value.reason_code == expected
    assert "DO_NOT_DISPLAY_PROVIDER_ERROR" not in repr(exc.value)


def test_rate_limit_clamped_and_retry_after_exposed_only_as_number():
    fake = FakeTransport(response({"error": "secret"}, status=429, headers={"Retry-After": "9999"}))
    with pytest.raises(ProviderError) as exc:
        provider(fake).list_torrents()
    assert exc.value.reason_code == "RATE_LIMITED"
    assert exc.value.retry_after == 120


def test_transport_exception_is_redacted():
    fake = FakeTransport(RuntimeError("SYNTHETIC_SECRET_NEVER_LOG"))
    with pytest.raises(ProviderError) as exc:
        provider(fake).account_status()
    assert exc.value.reason_code == "TRANSPORT_UNAVAILABLE"
    assert "SYNTHETIC_SECRET_NEVER_LOG" not in repr(exc.value)


def test_credential_provider_exception_is_redacted_and_has_zero_network_requests():
    fake = FakeTransport()
    p = RealDebridProvider(
        transport=fake, client_id="TEST_CLIENT_1234",
        access_token=lambda: (_ for _ in ()).throw(RuntimeError("SECRET_SHOULD_NOT_LEAK")),
    )
    with pytest.raises(ProviderError) as exc:
        p.account_status()
    assert exc.value.reason_code == "CREDENTIAL_UNAVAILABLE"
    assert "SECRET_SHOULD_NOT_LEAK" not in repr(exc.value)
    assert not fake.calls


def test_missing_token_denied_without_transport():
    fake = FakeTransport()
    with pytest.raises(ProviderError, match="CREDENTIAL_UNAVAILABLE"):
        provider(fake, token=None).list_downloads()
    assert not fake.calls


def test_non_json_is_rejected():
    fake = FakeTransport(response({}, headers={"Content-Type": "text/html"}))
    with pytest.raises(ProviderError, match="UNEXPECTED_CONTENT_TYPE"):
        provider(fake).account_status()


def test_malformed_response_is_rejected():
    fake = FakeTransport(ProviderResponse(200, "not json"))
    with pytest.raises(ProviderError, match="INVALID_RESPONSE"):
        provider(fake).account_status()


def test_no_torrent_write_apis_in_initial_source_slice():
    p = provider(FakeTransport())
    assert not hasattr(p, "add_magnet")
    assert not hasattr(p, "select_files")
    assert not hasattr(p, "unrestrict_link")
    assert not hasattr(p, "search_torrents")
    with pytest.raises(ProviderError, match="ENDPOINT_FORBIDDEN"):
        p._get("/rest/1.0/torrents/addMagnet")


def test_subscription_free_is_not_automatically_premium():
    assert provider(FakeTransport(response({"type": "free", "premium": 0}))).account_status() == AccountStatus("free", 0, None)


def test_no_media_selection_mutation():
    selected = {"job_id": "active-user-youtube-session", "endpoint": "tv"}
    provider(FakeTransport(response([]))).list_torrents()
    assert selected == {"job_id": "active-user-youtube-session", "endpoint": "tv"}


def test_premium_seconds_left_not_mistaken_for_epoch_expiry():
    data = {"type": "premium", "premium": 3600, "expiration": "2026-11-23T15:00:00.000Z"}
    result = provider(FakeTransport(response(data))).account_status()
    assert result.premium_seconds_left == 3600
    assert result.expiration_iso == "2026-11-23T15:00:00.000Z"


def test_premium_boolean_and_invalid_expiry_fail_closed():
    data = {"type": "premium", "premium": True, "expiration": "https://signed.invalid/?token=SECRET"}
    result = provider(FakeTransport(response(data))).account_status()
    assert result.premium_seconds_left is None
    assert result.expiration_iso is None
    assert "signed.invalid" not in repr(result)


def test_unexpected_torrent_status_is_normalized_without_leaking_url():
    rows = [{"id": "id123", "filename": "authorized.bin", "status": "https://signed.invalid/secret"}]
    result = provider(FakeTransport(response(rows))).list_torrents()
    assert result[0].status == "unknown"
    assert "signed.invalid" not in repr(result)
