"""Task 4: RS256 ID-token verification, the JWKS cache and the fake provider.

Everything here is pure: no network, an injected clock, and a fake provider
that signs with a throwaway test-only key through a second, independent RSA
implementation (tests/oidc_fake.py)."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import oidc_fake
from tests.oidc_fake import CLIENT_ID, ISSUER, FakeProvider
from volt import oidc_jwt
from volt.oidc_jwt import JwksCache, TokenError

NOW = 1_800_000_000
NONCE = "nonce-123"


def _verify(token, jwks, **kw):
    args = dict(issuer=ISSUER, audience=CLIENT_ID, nonce=NONCE, now=NOW)
    args.update(kw)
    return oidc_jwt.verify_id_token(token, jwks, **args)


def _code(case, token, jwks, **kw):
    with case.assertRaises(TokenError) as cm:
        _verify(token, jwks, **kw)
    return cm.exception.code


def _provider(**kw):
    return FakeProvider(now=lambda: NOW, **kw)


class Verify(unittest.TestCase):
    def test_valid_token_verifies(self):
        p = _provider()
        claims = _verify(p.id_token(NONCE, sub="alice"), p.jwks())
        self.assertEqual(claims["sub"], "alice")
        self.assertEqual(claims["iss"], ISSUER)
        self.assertEqual(claims["nonce"], NONCE)

    def test_alg_none_refused(self):
        p = _provider(alg="none")
        self.assertEqual(_code(self, p.id_token(NONCE), p.jwks()), "alg_unsupported")

    def test_hs256_signed_with_public_key_refused(self):
        p = _provider(alg="HS256")
        self.assertEqual(_code(self, p.id_token(NONCE), p.jwks()), "alg_unsupported")

    def test_es256_refused_alg_unsupported(self):
        p = _provider(alg="ES256")
        self.assertEqual(_code(self, p.id_token(NONCE), p.jwks()), "alg_unsupported")

    def test_lowercase_or_missing_alg_refused(self):
        p = _provider(alg="rs256")
        self.assertEqual(_code(self, p.id_token(NONCE), p.jwks()), "alg_unsupported")
        p = _provider()
        h, c, s = p.id_token(NONCE).split(".")
        no_alg = oidc_fake.b64u(b'{"kid":"%s"}' % p.kid.encode())
        self.assertEqual(_code(self, f"{no_alg}.{c}.{s}", p.jwks()), "alg_unsupported")

    def test_wrong_issuer_audience_nonce_each_refused(self):
        p = _provider()
        good = p.id_token(NONCE)
        self.assertEqual(_code(self, good, p.jwks(), issuer=ISSUER + "/x"), "bad_claim:iss")
        self.assertEqual(_code(self, good, p.jwks(), audience="other"), "bad_claim:aud")
        self.assertEqual(_code(self, good, p.jwks(), nonce="other"), "bad_claim:nonce")
        self.assertEqual(_code(self, good, p.jwks(), nonce=""), "bad_claim:nonce")

    def test_missing_claims_refused(self):
        p = _provider()
        for name in ("iss", "aud", "exp", "iat", "nonce", "sub"):
            tok = p.id_token(NONCE, drop=(name,))
            self.assertEqual(_code(self, tok, p.jwks()), "bad_claim:" + name, name)

    def test_claim_types_are_checked(self):
        p = _provider()
        for name, value in (("exp", "9999999999"), ("exp", True), ("iat", None),
                            ("iss", ["x"]), ("nonce", 5), ("sub", "")):
            if name == "nonce":
                tok = p.id_token(value)
            else:
                tok = p.id_token(NONCE, **{name: value})
            self.assertEqual(_code(self, tok, p.jwks()), "bad_claim:" + name, (name, value))

    def test_expired_and_far_future_and_stale_iat(self):
        p = _provider()
        self.assertEqual(_code(self, _provider(expired=True).id_token(NONCE), p.jwks()),
                         "bad_claim:exp")
        self.assertEqual(_code(self, _provider(old_iat=True).id_token(NONCE), p.jwks()),
                         "bad_claim:iat")
        far = p.id_token(NONCE, iat=NOW + 3600, exp=NOW + 7200)
        self.assertEqual(_code(self, far, p.jwks()), "bad_claim:iat")
        # a stale iat is refused even when exp is still in the future
        stale = p.id_token(NONCE, iat=NOW - 601, exp=NOW + 3000)
        self.assertEqual(_code(self, stale, p.jwks()), "bad_claim:iat")
        fresh_enough = p.id_token(NONCE, iat=NOW - 599, exp=NOW + 3000)
        self.assertEqual(_verify(fresh_enough, p.jwks())["sub"], "user-1")

    def test_nbf_in_the_future_refused(self):
        p = _provider()
        self.assertEqual(_code(self, p.id_token(NONCE, nbf=NOW + 500), p.jwks()),
                         "bad_claim:nbf")
        self.assertEqual(_verify(p.id_token(NONCE, nbf=NOW - 5), p.jwks())["sub"], "user-1")

    def test_skew_leeway_boundary(self):
        p = _provider()
        tok = p.id_token(NONCE, iat=NOW, exp=NOW + 300)
        jwks = p.jwks()
        # clock behind the issuer: iat appears in the future
        self.assertTrue(_verify(tok, jwks, now=NOW - 119))
        self.assertEqual(_code(self, tok, jwks, now=NOW - 121), "bad_claim:iat")
        # clock ahead of the issuer: exp appears in the past
        self.assertTrue(_verify(tok, jwks, now=NOW + 300 + 119))
        self.assertEqual(_code(self, tok, jwks, now=NOW + 300 + 121), "bad_claim:exp")
        # configurable
        self.assertTrue(_verify(tok, jwks, now=NOW - 200, leeway=250))

    def test_azp_required_when_aud_is_a_list(self):
        p = _provider()
        jwks = p.jwks()
        self.assertEqual(_code(self, p.id_token(NONCE, aud=[CLIENT_ID, "x"]), jwks),
                         "bad_claim:azp")
        self.assertEqual(
            _code(self, p.id_token(NONCE, aud=[CLIENT_ID, "x"], azp="x"), jwks),
            "bad_claim:azp")
        self.assertTrue(_verify(p.id_token(NONCE, aud=[CLIENT_ID, "x"], azp=CLIENT_ID), jwks))
        self.assertEqual(_code(self, p.id_token(NONCE, aud=["x", "y"], azp="x"), jwks),
                         "bad_claim:aud")
        self.assertEqual(_code(self, p.id_token(NONCE, aud=[]), jwks), "bad_claim:aud")

    def test_malformed_tokens(self):
        p = _provider()
        good = p.id_token(NONCE)
        h, c, s = good.split(".")
        for bad in ("", "a.b", "a.b.c.d", f"{h}.{c}", f"{h}.{c}.{s}.", "....",
                    f"!!!.{c}.{s}", f"{h}.{c}=.{s}", f"{h}.{c} .{s}",
                    f"{oidc_fake.b64u(b'not json')}.{c}.{s}",
                    f"{oidc_fake.b64u(b'[]')}.{c}.{s}",
                    f"{oidc_fake.b64u(b'{\"alg\":\"RS256\",\"alg\":\"none\"}')}.{c}.{s}",
                    "x" * 100_000, None, 5, b"a.b.c"):
            self.assertEqual(_code(self, bad, p.jwks()), "malformed", repr(bad)[:40])

    def test_unknown_critical_header_refused(self):
        p = _provider()
        self.assertEqual(_code(self, p.id_token(NONCE, header={"crit": ["exp"]}), p.jwks()),
                         "malformed")

    def test_constant_time_comparison_is_used(self):
        p = _provider()
        real = oidc_jwt.hmac.compare_digest
        with mock.patch.object(oidc_jwt.hmac, "compare_digest", side_effect=real) as m:
            _verify(p.id_token(NONCE), p.jwks())
        self.assertGreaterEqual(m.call_count, 2)  # the encoded signature block and the nonce

    def test_every_fake_misbehaviour_is_refused(self):
        stale_jwks = _provider().jwks()
        cases = {
            "bad_nonce": "bad_claim:nonce",
            "bad_aud": "bad_claim:aud",
            "bad_iss": "bad_claim:iss",
            "expired": "bad_claim:exp",
            "old_iat": "bad_claim:iat",
            "truncate_sig": "bad_signature",
            "forge_e3": "bad_key",
            "small_key": "bad_key",
        }
        for switch, code in cases.items():
            p = _provider(**{switch: True})
            self.assertEqual(_code(self, p.id_token(NONCE), p.jwks()), code, switch)
        p = _provider(kid="not-in-set")
        self.assertEqual(_code(self, p.id_token(NONCE), p.jwks()), "unknown_kid")
        p = _provider(rotate_keys=True)
        self.assertEqual(_code(self, p.id_token(NONCE), stale_jwks), "unknown_kid")
        self.assertTrue(_verify(p.id_token(NONCE), p.jwks()))

    def test_a_token_signed_by_another_key_with_the_same_kid_is_refused(self):
        p = _provider()
        other = _provider(rotate_keys=True, kid=p.kid)
        self.assertEqual(_code(self, other.id_token(NONCE), p.jwks()), "bad_signature")

    def test_tampered_payload_refused(self):
        p = _provider()
        h, c, s = p.id_token(NONCE, sub="alice").split(".")
        evil = oidc_fake.b64u(oidc_fake.b64d(c).replace(b"alice", b"admin"))
        self.assertEqual(_code(self, f"{h}.{evil}.{s}", p.jwks()), "bad_signature")


class Rsa(unittest.TestCase):
    def setUp(self):
        self.key = oidc_fake.MAIN
        self.n, self.e = self.key[0], self.key[1]
        self.k = (self.n.bit_length() + 7) // 8
        self.msg = b"header.payload"

    def _raw_sign(self, em):
        n, e, d = self.key[0], self.key[1], self.key[2]
        return pow(int.from_bytes(em, "big"), d, n).to_bytes(self.k, "big")

    def _em(self, tail, ff=None):
        ff = self.k - 3 - len(tail) if ff is None else ff
        return b"\x00\x01" + b"\xff" * ff + b"\x00" + tail

    def test_good_signature_verifies(self):
        sig = oidc_fake.sign_rs256(self.key, self.msg)
        self.assertTrue(oidc_jwt.rsa_verify_rs256(self.n, self.e, self.msg, sig))
        self.assertFalse(oidc_jwt.rsa_verify_rs256(self.n, self.e, self.msg + b"x", sig))

    def test_wrong_length_and_truncated_signature_refused(self):
        sig = oidc_fake.sign_rs256(self.key, self.msg)
        for bad in (sig[:-1], sig[1:], b"\x00" + sig, sig + b"\x00", b"", sig[: self.k // 2]):
            self.assertFalse(oidc_jwt.rsa_verify_rs256(self.n, self.e, self.msg, bad))

    def test_signature_not_below_modulus_refused(self):
        # s + n has the same residue and the same length only when it fits; make it fit
        sig = int.from_bytes(oidc_fake.sign_rs256(self.key, self.msg), "big")
        if sig + self.n < 1 << (8 * self.k):
            wrapped = (sig + self.n).to_bytes(self.k, "big")
            self.assertFalse(oidc_jwt.rsa_verify_rs256(self.n, self.e, self.msg, wrapped))

    def test_trailing_garbage_after_the_digest_refused(self):
        tail = oidc_fake.digest_info_sha256(self.msg)
        # A lenient parser that stops after the digest would accept all of these.
        for junk in (b"\x00", b"garbage", b"\x00" * 8, b"\xff" * 20):
            em = self._em(tail + junk)
            sig = self._raw_sign(em)
            self.assertFalse(oidc_jwt.rsa_verify_rs256(self.n, self.e, self.msg, sig), junk)
        # control: the same machinery with no junk is accepted
        self.assertTrue(oidc_jwt.rsa_verify_rs256(
            self.n, self.e, self.msg, self._raw_sign(self._em(tail))))

    def test_malformed_padding_variants_refused(self):
        tail = oidc_fake.digest_info_sha256(self.msg)
        digest = hashlib.sha256(self.msg).digest()
        variants = {
            "short ff run with filler": b"\x00\x01" + b"\xff" * 7 + b"\x00" + tail
            + b"\x00" * (self.k - 3 - 7 - len(tail)),
            "no null parameters": b"\x00\x01" + b"\xff" * (self.k - 3 - 49) + b"\x00"
            + bytes.fromhex("302f300b06096086480165030402010420") + digest,
            "wrong block type": b"\x00\x02" + b"\xff" * (self.k - 3 - len(tail)) + b"\x00" + tail,
            "nonzero lead": b"\x01\x01" + b"\xff" * (self.k - 3 - len(tail)) + b"\x00" + tail,
            "non-ff pad byte": b"\x00\x01" + b"\xfe" + b"\xff" * (self.k - 4 - len(tail))
            + b"\x00" + tail,
            "sha1 digestinfo": b"\x00\x01" + b"\xff" * (self.k - 3 - 35) + b"\x00"
            + bytes.fromhex("3021300906052b0e03021a05000414") + hashlib.sha1(self.msg).digest(),
        }
        for name, em in variants.items():
            self.assertEqual(len(em), self.k, name)
            self.assertFalse(oidc_jwt.rsa_verify_rs256(
                self.n, self.e, self.msg, self._raw_sign(em)), name)

    def test_bleichenbacher_style_forgery_refused(self):
        n3, e3 = oidc_fake.E3[0], oidc_fake.E3[1]
        self.assertEqual(e3, 3)
        sig = oidc_fake.forge_e3_signature(oidc_fake.E3, self.msg)
        # The forgery really is what a lenient parser would accept ...
        k = (n3.bit_length() + 7) // 8
        block = pow(int.from_bytes(sig, "big"), 3, n3).to_bytes(k, "big")
        self.assertTrue(block.startswith(b"\x00\x01\xff\x00" + oidc_fake.digest_info_sha256(self.msg)))
        # ... and the verifier, which compares the whole block, refuses it.
        self.assertFalse(oidc_jwt.rsa_verify_rs256(n3, e3, self.msg, sig))

    def test_openssl_cross_check(self):
        exe = shutil.which("openssl")
        if not exe:
            self.skipTest("openssl not installed")
        with tempfile.TemporaryDirectory() as tmp:
            keyfile = os.path.join(tmp, "k.pem")
            msgfile = os.path.join(tmp, "m.bin")
            sigfile = os.path.join(tmp, "s.bin")
            with open(keyfile, "w", newline="\n") as f:
                f.write(oidc_fake.pem_private(self.key))
            with open(msgfile, "wb") as f:
                f.write(self.msg)
            subprocess.run([exe, "dgst", "-sha256", "-sign", keyfile, "-out", sigfile, msgfile],
                           check=True, capture_output=True)
            with open(sigfile, "rb") as f:
                sig = f.read()
        self.assertTrue(oidc_jwt.rsa_verify_rs256(self.n, self.e, self.msg, sig))
        # and our independent signer agrees byte for byte with openssl
        self.assertEqual(sig, oidc_fake.sign_rs256(self.key, self.msg))


class Keys(unittest.TestCase):
    def _jwk(self, **over):
        p = _provider()
        jwk = dict(p.jwks()["keys"][0])
        jwk.update(over)
        return p, {"keys": [jwk]}

    def test_small_modulus_and_small_exponent_refused(self):
        p, _ = self._jwk()
        tok = p.id_token(NONCE)
        n_b = oidc_fake.MAIN[0]
        tests = {
            "1024 bit": oidc_fake.jwk(oidc_fake.SMALL, p.kid),
            "2047 bit": dict(oidc_fake.jwk(oidc_fake.MAIN, p.kid),
                             n=oidc_fake.b64u_int((n_b >> 1) | 1)),
            "e=3": oidc_fake.jwk(oidc_fake.E3, p.kid),
            "e=1": dict(oidc_fake.jwk(oidc_fake.MAIN, p.kid), e=oidc_fake.b64u_int(1)),
            "e=65535": dict(oidc_fake.jwk(oidc_fake.MAIN, p.kid), e=oidc_fake.b64u_int(65535)),
            "even e": dict(oidc_fake.jwk(oidc_fake.MAIN, p.kid), e=oidc_fake.b64u_int(65538)),
            "even n": dict(oidc_fake.jwk(oidc_fake.MAIN, p.kid),
                           n=oidc_fake.b64u_int(n_b + 1)),
            "huge e": dict(oidc_fake.jwk(oidc_fake.MAIN, p.kid),
                           e=oidc_fake.b64u_int(1 << 70 | 1)),
        }
        for name, jwk in tests.items():
            self.assertEqual(_code(self, tok, {"keys": [jwk]}), "bad_key", name)

    def test_unusable_jwk_shapes_refused(self):
        p, _ = self._jwk()
        tok = p.id_token(NONCE)
        for over in ({"kty": "EC"}, {"kty": "oct"}, {"n": 5}, {"e": None}, {"n": "***"},
                     {"use": "enc"}, {"alg": "RS512"}):
            _, jwks = self._jwk(**over)
            self.assertEqual(_code(self, tok, jwks), "bad_key", over)
        for jwks in ({}, {"keys": "x"}, {"keys": [5]}, [], None):
            self.assertEqual(_code(self, tok, jwks), "unknown_kid", repr(jwks))

    def test_duplicate_kid_is_ambiguous_and_refused(self):
        p, _ = self._jwk()
        jwk = oidc_fake.jwk(oidc_fake.MAIN, p.kid)
        evil = oidc_fake.jwk(oidc_fake.ROTATED, p.kid)
        self.assertEqual(_code(self, p.id_token(NONCE), {"keys": [jwk, evil]}), "bad_key")

    def test_signature_checked_before_claims_are_trusted(self):
        # a bad signature with a bad claim reports the signature, never leaks claim checks
        p = _provider(truncate_sig=True, bad_aud=True)
        self.assertEqual(_code(self, p.id_token(NONCE), p.jwks()), "bad_signature")


class Cache(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.p = _provider()
        self.calls = []
        self.fail = False
        self.max_age = 600

        def fetch(provider):
            self.calls.append((provider, self.t[0]))
            if self.fail:
                raise OSError("down")
            return self.p.jwks(), self.max_age

        self.cache = JwksCache(fetch, lambda: self.t[0])

    def test_known_kid_is_served_from_cache(self):
        self.assertTrue(_verify(self.p.id_token(NONCE), self.cache.keys("g", self.p.kid)))
        self.cache.keys("g", self.p.kid)
        self.t[0] += 599
        self.cache.keys("g", self.p.kid)
        self.assertEqual(len(self.calls), 1)
        self.t[0] += 2
        self.cache.keys("g", self.p.kid)
        self.assertEqual(len(self.calls), 2)

    def test_providers_are_cached_separately(self):
        self.cache.keys("g", self.p.kid)
        self.cache.keys("m", self.p.kid)
        self.assertEqual([c[0] for c in self.calls], ["g", "m"])

    def test_max_age_is_clamped(self):
        self.max_age = 5
        self.cache.keys("g", self.p.kid)
        self.t[0] += 299
        self.cache.keys("g", self.p.kid)
        self.assertEqual(len(self.calls), 1)  # floor 300
        self.t[0] += 2
        self.cache.keys("g", self.p.kid)
        self.assertEqual(len(self.calls), 2)
        self.max_age = 10 ** 9
        self.t[0] += 301
        self.cache.keys("g", self.p.kid)
        self.assertEqual(len(self.calls), 3)
        self.t[0] += 86399
        self.cache.keys("g", self.p.kid)
        self.assertEqual(len(self.calls), 3)  # ceiling 24 h
        self.t[0] += 2
        self.cache.keys("g", self.p.kid)
        self.assertEqual(len(self.calls), 4)

    def test_unknown_kid_one_refetch_then_refused(self):
        self.cache.keys("g", self.p.kid)
        with self.assertRaises(TokenError) as cm:
            self.cache.keys("g", "forged-1")
        self.assertEqual(cm.exception.code, "unknown_kid")
        self.assertEqual(len(self.calls), 2)  # warm-up plus exactly one refetch
        self.t[0] += 10
        with self.assertRaises(TokenError) as cm:
            self.cache.keys("g", "forged-2")
        self.assertEqual(cm.exception.code, "unknown_kid")
        self.assertEqual(len(self.calls), 2)  # within 60 s: no second fetch
        for i in range(50):
            with self.assertRaises(TokenError):
                self.cache.keys("g", f"spray-{i}")
        self.assertEqual(len(self.calls), 2)
        self.t[0] += 51
        with self.assertRaises(TokenError):
            self.cache.keys("g", "forged-3")
        self.assertEqual(len(self.calls), 3)

    def test_rotated_keys_picked_up(self):
        self.cache.keys("g", self.p.kid)
        self.p = _provider(rotate_keys=True)
        self.t[0] += 5
        # first sighting of the new kid beyond the refetch gap from the warm-up? it is not,
        # so move past it as a real rotation would
        self.t[0] += 60
        jwks = self.cache.keys("g", self.p.kid)
        self.assertTrue(_verify(self.p.id_token(NONCE), jwks))
        self.assertEqual(len(self.calls), 2)

    def test_jwks_stale_while_error_up_to_24h(self):
        self.cache.keys("g", self.p.kid)  # expires at 1600
        self.fail = True
        self.t[0] = 1000 + 601
        jwks = self.cache.keys("g", self.p.kid)  # stale but served
        self.assertTrue(_verify(self.p.id_token(NONCE), jwks))
        self.t[0] = 1000 + 600 + 86400 - 1
        self.assertTrue(self.cache.keys("g", self.p.kid))
        self.t[0] = 1000 + 600 + 86400 + 1
        with self.assertRaises(TokenError) as cm:
            self.cache.keys("g", self.p.kid)
        self.assertEqual(cm.exception.code, "unavailable")

    def test_unknown_kid_while_provider_down_is_unavailable(self):
        self.cache.keys("g", self.p.kid)
        self.fail = True
        self.t[0] += 61
        with self.assertRaises(TokenError) as cm:
            self.cache.keys("g", "forged")
        self.assertEqual(cm.exception.code, "unavailable")

    def test_outage_does_not_hammer_the_provider(self):
        self.cache.keys("g", self.p.kid)
        self.fail = True
        self.t[0] += 700  # expired, provider down
        for _ in range(30):
            self.cache.keys("g", self.p.kid)
        self.assertEqual(len(self.calls), 2)  # warm-up plus one failed retry inside the gap
        self.t[0] += 61
        self.cache.keys("g", self.p.kid)
        self.assertEqual(len(self.calls), 3)

    def test_never_fetched_and_down_is_unavailable_and_rate_limited(self):
        self.fail = True
        for _ in range(5):
            with self.assertRaises(TokenError) as cm:
                self.cache.keys("g", self.p.kid)
            self.assertEqual(cm.exception.code, "unavailable")
        self.assertEqual(len(self.calls), 1)

    def test_garbage_from_the_provider_is_not_cached(self):
        good = self.cache.keys("g", self.p.kid)
        self.t[0] += 700
        self.p.jwks = lambda: {"keys": "nonsense"}
        self.assertEqual(self.cache.keys("g", self.p.kid), good)  # stale-while-error


class FakeProviderEndpoints(unittest.TestCase):
    def test_discovery_and_jwks_through_the_http_seam(self):
        p = _provider()
        disc = p.get_json(ISSUER + "/.well-known/openid-configuration")
        self.assertEqual(disc["issuer"], ISSUER)
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            self.assertTrue(disc[key].startswith(ISSUER))
        self.assertEqual(p.get_json(disc["jwks_uri"]), p.jwks())
        self.assertEqual([r[0] for r in p.requests], ["GET", "GET"])
        with self.assertRaises(KeyError):
            p.get_json(ISSUER + "/nope")

    def test_token_endpoint_checks_code_pkce_and_binds_nonce(self):
        import base64
        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()
                                             ).rstrip(b"=").decode()
        p = _provider()
        code = p.authorize({"nonce": NONCE, "code_challenge": challenge,
                            "code_challenge_method": "S256", "client_id": CLIENT_ID})
        disc = p.discovery()
        data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                "client_id": CLIENT_ID}
        resp = p.post_form(disc["token_endpoint"], data)
        self.assertEqual(_verify(resp["id_token"], p.jwks())["nonce"], NONCE)
        # a code is single use
        resp2 = p.post_form(disc["token_endpoint"], data)
        self.assertEqual(resp2.get("error"), "invalid_grant")
        # wrong verifier
        code = p.authorize({"nonce": NONCE, "code_challenge": challenge,
                            "code_challenge_method": "S256", "client_id": CLIENT_ID})
        bad = p.post_form(disc["token_endpoint"], dict(data, code=code, code_verifier="x" * 64))
        self.assertEqual(bad.get("error"), "invalid_grant")


if __name__ == "__main__":
    unittest.main()
