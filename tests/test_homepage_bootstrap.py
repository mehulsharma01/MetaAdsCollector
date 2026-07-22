"""Tests for the facebook.com homepage bootstrap and the cookies argument.

The Ad Library page (``/ads/library/``) now answers non-browser clients with
a 403 JS challenge, so ``initialize()`` bootstraps from the logged-out
facebook.com homepage instead.  These tests cover:

- mining ``lsd`` / ``__spin_r`` (rev) / ``hsi`` tokens from homepage HTML
- capturing the server cookies (``datr``/``fr``/``sb``) the homepage sets
- hard failure (``AuthenticationError``) when no genuine ``lsd`` is found
- falling back to the hardcoded doc_id constants when the homepage HTML
  carries no Ad Library doc_ids
- the ``cookies`` constructor argument (dict and header-string forms)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meta_ads_collector.client import MetaAdsClient
from meta_ads_collector.constants import DOC_ID_SEARCH
from meta_ads_collector.exceptions import AuthenticationError

# A realistic logged-out homepage blob: an LSD token in the standard
# ["LSD",[],{"token":"..."}] shape, plus __spin_r (build revision) and hsi.
HOMEPAGE_HTML = (
    '<!DOCTYPE html><html><head><title>Facebook</title></head><body>'
    '<script type="application/json" data-content-len="240">'
    '{"require":[["LSD",[],{"token":"AVeryRealLSD123"}],'
    '["SiteData",[],{"__spin_r":1023456789,"__spin_b":"trunk",'
    '"__spin_t":1700000000,"hsi":"7123456789012345678",'
    '"server_revision":1023456789}]]}'
    "</script></body></html>"
)

EXPECTED_JAZOEST = str(2 + sum(ord(c) for c in "AVeryRealLSD123"))


def _fake_response(status: int = 200, text: str = "") -> MagicMock:
    """Build a mock curl_cffi response."""
    response = MagicMock()
    response.status_code = status
    response.text = text
    return response


def _make_homepage_client(
    html: str = HOMEPAGE_HTML,
    status: int = 200,
    server_cookies: dict[str, str] | None = None,
    **client_kwargs,
) -> MetaAdsClient:
    """Build a client whose session.request serves fake homepage HTML.

    *server_cookies* are planted in the session jar when the request is
    made, mimicking the ``Set-Cookie`` processing of a real response.
    """
    client = MetaAdsClient(**client_kwargs)

    def fake_request(*args, **kwargs):
        for name, value in (server_cookies or {}).items():
            client.session.cookies.set(name, value, domain=".facebook.com", path="/")
        return _fake_response(status=status, text=html)

    client.session.request = MagicMock(side_effect=fake_request)
    return client


class TestHomepageBootstrap:
    """Sync ``MetaAdsClient.initialize()`` against a mocked homepage."""

    def test_home_url_constant(self):
        """The bootstrap target is the facebook.com homepage."""
        assert MetaAdsClient.HOME_URL == "https://www.facebook.com/"

    def test_initialize_gets_homepage_not_ad_library(self):
        """The bootstrap GET targets HOME_URL, never /ads/library/."""
        client = _make_homepage_client()
        with patch("meta_ads_collector.client.time.sleep"):
            assert client.initialize() is True
        url = client.session.request.call_args.kwargs["url"]
        assert url == MetaAdsClient.HOME_URL
        assert url != MetaAdsClient.AD_LIBRARY_URL

    def test_initialize_mines_tokens_from_homepage(self):
        """lsd/__spin_r/hsi are extracted and jazoest derived from lsd."""
        client = _make_homepage_client()
        with patch("meta_ads_collector.client.time.sleep"):
            assert client.initialize() is True
        assert client._tokens["lsd"] == "AVeryRealLSD123"
        assert client._tokens["__rev"] == "1023456789"
        assert client._tokens["__spin_r"] == "1023456789"
        assert client._tokens["__hsi"] == "7123456789012345678"
        assert client._tokens["jazoest"] == EXPECTED_JAZOEST
        assert client._initialized is True

    def test_initialize_captures_server_cookies(self):
        """Cookies set by the homepage response land in the session jar."""
        server_cookies = {"datr": "datr_val", "fr": "fr_val", "sb": "sb_val"}
        client = _make_homepage_client(server_cookies=server_cookies)
        with patch("meta_ads_collector.client.time.sleep"):
            assert client.initialize() is True
        for name, value in server_cookies.items():
            assert client.session.cookies.get(name) == value
        # The viewport cookies the client seeds itself are present too.
        assert client.session.cookies.get("wd") is not None
        assert client.session.cookies.get("dpr") is not None

    def test_initialize_raises_when_homepage_lacks_lsd(self):
        """Homepage HTML without a genuine lsd is a hard failure."""
        html = '<html><body>"__spin_r":1023456789 but no LSD token</body></html>'
        client = _make_homepage_client(html=html)
        with (
            patch("meta_ads_collector.client.time.sleep"),
            pytest.raises(AuthenticationError, match="LSD"),
        ):
            client.initialize()
        assert client._initialized is False
        # No lsd may be fabricated as a side effect.
        assert "lsd" not in client._tokens

    def test_initialize_raises_on_homepage_error_status(self):
        """A non-200 homepage response raises AuthenticationError."""
        client = _make_homepage_client(status=403)
        with pytest.raises(AuthenticationError, match="Homepage bootstrap failed"):
            client.initialize()
        assert client._initialized is False

    def test_doc_ids_fall_back_to_constants(self):
        """Homepage HTML has no Ad Library doc_ids -> constants are used."""
        search_json = (
            '{"data":{"ad_library_main":{"search_results_connection":'
            '{"edges":[],"page_info":{"has_next_page":false}}}}}'
        )
        client = _make_homepage_client()
        client.session.request = MagicMock(
            side_effect=[
                _fake_response(text=HOMEPAGE_HTML),
                _fake_response(text=search_json),
            ]
        )
        with patch("meta_ads_collector.client.time.sleep"):
            result, cursor = client.search_ads(query="test")
        # Nothing dynamically extracted from the homepage...
        assert client._doc_ids == {}
        # ...so the GraphQL POST used the hardcoded constant.
        post_call = client.session.request.call_args_list[1]
        assert post_call.kwargs["data"]["doc_id"] == DOC_ID_SEARCH
        assert result["ads"] == []
        assert cursor is None


class TestProvidedCookies:
    """The ``cookies`` constructor argument (sync client)."""

    def test_dict_cookies_seeded_into_session(self):
        client = MetaAdsClient(cookies={"c_user": "12345", "xs": "secret"})
        assert client.session.cookies.get("c_user") == "12345"
        assert client.session.cookies.get("xs") == "secret"

    def test_header_string_cookies_seeded_into_session(self):
        client = MetaAdsClient(cookies="c_user=12345; xs=secret; fr=abc=def")
        assert client.session.cookies.get("c_user") == "12345"
        assert client.session.cookies.get("xs") == "secret"
        # Values containing "=" are kept intact (split on first "=").
        assert client.session.cookies.get("fr") == "abc=def"

    def test_no_cookies_means_empty_provided_set(self):
        client = MetaAdsClient()
        assert client._provided_cookies == {}

    def test_parse_cookies_normalises_values_to_str(self):
        assert MetaAdsClient._parse_cookies(None) == {}
        assert MetaAdsClient._parse_cookies("") == {}
        assert MetaAdsClient._parse_cookies({"a": 1}) == {"a": "1"}
        # Entries without "=" in a header string are skipped.
        assert MetaAdsClient._parse_cookies("lonely; k=v") == {"k": "v"}

    def test_cookies_reapplied_on_session_refresh(self):
        """Session refresh builds a fresh jar; provided cookies are re-seeded."""
        client = MetaAdsClient(cookies={"c_user": "12345"})
        with patch.object(client, "initialize", return_value=True):
            assert client._refresh_session() is True
        assert client.session.cookies.get("c_user") == "12345"


class TestAsyncHomepageBootstrap:
    """Async ``AsyncMetaAdsClient.initialize()`` against a mocked homepage."""

    @pytest.mark.asyncio
    async def test_async_initialize_mines_tokens_from_homepage(self):
        from meta_ads_collector.async_client import AsyncMetaAdsClient

        client = AsyncMetaAdsClient()
        client._make_request = AsyncMock(
            return_value=_fake_response(text=HOMEPAGE_HTML)
        )
        try:
            assert await client.initialize() is True
            assert client._tokens["lsd"] == "AVeryRealLSD123"
            assert client._tokens["__spin_r"] == "1023456789"
            assert client._tokens["__hsi"] == "7123456789012345678"
            assert client._tokens["jazoest"] == EXPECTED_JAZOEST
            assert client._initialized is True
            # The bootstrap GET targeted the homepage.
            assert client._make_request.call_args.args[1] == MetaAdsClient.HOME_URL
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_async_initialize_raises_when_homepage_lacks_lsd(self):
        from meta_ads_collector.async_client import AsyncMetaAdsClient

        client = AsyncMetaAdsClient()
        client._make_request = AsyncMock(
            return_value=_fake_response(text="<html><body>no tokens</body></html>")
        )
        try:
            with pytest.raises(AuthenticationError, match="lsd"):
                await client.initialize()
            assert client._initialized is False
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_async_initialize_raises_on_homepage_error_status(self):
        from meta_ads_collector.async_client import AsyncMetaAdsClient

        client = AsyncMetaAdsClient()
        client._make_request = AsyncMock(return_value=_fake_response(status=403))
        try:
            with pytest.raises(AuthenticationError, match="homepage"):
                await client.initialize()
            assert client._initialized is False
        finally:
            await client.close()


class TestAsyncProvidedCookies:
    """The ``cookies`` constructor argument (async client)."""

    @pytest.mark.asyncio
    async def test_async_dict_cookies_seeded_into_session(self):
        from meta_ads_collector.async_client import AsyncMetaAdsClient

        client = AsyncMetaAdsClient(cookies={"c_user": "12345", "xs": "secret"})
        try:
            assert client._client.cookies.get("c_user") == "12345"
            assert client._client.cookies.get("xs") == "secret"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_async_header_string_cookies_seeded_into_session(self):
        from meta_ads_collector.async_client import AsyncMetaAdsClient

        client = AsyncMetaAdsClient(cookies="c_user=12345; xs=secret")
        try:
            assert client._client.cookies.get("c_user") == "12345"
            assert client._client.cookies.get("xs") == "secret"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_async_cookies_reseeded_on_client_rebuild(self):
        """A rebuilt (refreshed) async client keeps the provided cookies."""
        from meta_ads_collector.async_client import AsyncMetaAdsClient

        client = AsyncMetaAdsClient(cookies={"c_user": "12345"})
        try:
            await client._rebuild_client()
            assert client._client.cookies.get("c_user") == "12345"
        finally:
            await client.close()
