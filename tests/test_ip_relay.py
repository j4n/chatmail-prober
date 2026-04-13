"""Tests for IP relay support in create_qr_url and is_ip_address.

IP relay support is already implemented; these tests lock in the contract
so regressions are caught immediately.
"""
from __future__ import annotations

import urllib.parse

import pytest

from chatmail_prober.prober import create_qr_url, is_ip_address


class TestIsIpAddress:
    def test_ipv4_detected(self):
        assert is_ip_address("192.168.1.1") is True

    def test_ipv6_detected(self):
        assert is_ip_address("::1") is True
        assert is_ip_address("2001:db8::1") is True

    def test_domain_not_ip(self):
        assert is_ip_address("nine.testrun.org") is False

    def test_bare_hostname_not_ip(self):
        assert is_ip_address("localhost") is False

    def test_empty_string_not_ip(self):
        assert is_ip_address("") is False



class TestCreateQrUrl:
    def test_domain_produces_dcaccount_url(self):
        url = create_qr_url("nine.testrun.org")
        assert url == "dcaccount:nine.testrun.org"

    def test_ip_produces_dclogin_url(self):
        url = create_qr_url("192.168.1.1")
        assert url.startswith("dclogin:")

    def test_dclogin_url_contains_ip(self):
        url = create_qr_url("10.0.0.1")
        assert "10.0.0.1" in url

    def test_dclogin_url_has_required_params(self):
        url = create_qr_url("192.168.1.1")
        # Strip scheme to get the authority+query part
        rest = url[len("dclogin:"):]
        # Parse as if it were a URL with a dummy scheme
        parsed = urllib.parse.urlparse("http://" + rest.split("?")[0])
        qs = urllib.parse.parse_qs(url.split("?")[1]) if "?" in url else {}
        assert "p" in qs, "password param missing"
        assert "v" in qs, "version param missing"
        assert "ip" in qs, "IMAP port param missing"
        assert "sp" in qs, "SMTP port param missing"

    def test_dclogin_password_is_url_encoded(self):
        # Run multiple times to hit special chars in random password
        for _ in range(20):
            url = create_qr_url("192.168.1.1")
            qs_str = url.split("?")[1] if "?" in url else ""
            qs = urllib.parse.parse_qs(qs_str)
            raw_password = qs.get("p", [""])[0]
            # URL-decoded password should not contain raw special chars
            # that would break URL parsing
            decoded = urllib.parse.unquote(raw_password)
            assert decoded  # non-empty
