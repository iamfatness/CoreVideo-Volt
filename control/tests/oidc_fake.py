"""A fake OpenID Connect provider for tests. No network, no dependencies.

TEST ONLY. The RSA keys below are throwaway keys generated once for this
file. They protect nothing, are trusted by nothing outside the test suite, and
must never be used (or copied) anywhere else.

Signing here is a deliberately independent implementation of RS256: it builds
the DigestInfo with a real DER encoder and signs with the CRT, where the
verifier in volt/oidc_jwt.py uses a fixed prefix and compares whole blocks.
Two implementations that agree byte for byte (and with openssl) are the proof.

Misbehaviour switches (constructor keywords or plain attributes):
  bad_nonce, bad_aud, bad_iss, expired, old_iat   wrong or stale claims
  alg=<name>        header alg: "none", "HS256" (MAC keyed with the public key), others
  kid=<text>        header kid that the JWKS does not hold
  rotate_keys       sign with, and publish, the second key (a provider rotation)
  truncate_sig      drop the last signature byte
  forge_e3          publish an e=3 key and sign with a Bleichenbacher-style forgery
  small_key         publish and sign with a 1024-bit key
"""

import base64
import hashlib
import hmac
import json
import time

ISSUER = "https://idp.test"
CLIENT_ID = "volt-test-client"

# (n, e, d, p, q).  TEST-ONLY throwaway keys.
MAIN = (
    0xc7baf7fb4e217adba1e816d2ee648c5982eda8e36a3d9fe9096fa8307f0dea4c53fbf5761eff56b3c9a66f1928d092feb90a7b6466c144919a8884c5a85d3ee2c54a0960b47dfa1879f734b3d426e9958c98f2138baa46c73554b5458f96855ed9215ebfeaabede884e618901e3cf35c0b7ecdfc7ff9e5d4d73853f0812e13b4e0035ed02e685ca6c95134c419d96e32351b63005881b1bf8292cec731f3ce93b04cc6063fa15585684c85ced2b1ee6203f9568946ceee5c957967243ca10d73700c544e5eb2700749c9f3180f30b6c7a01a3303a05968cd9e17aa942e05b2ba7e82de3b4074a85d96eaf53019a220c44ad72a6c1edead242a0719653d086b93,
    0x10001,
    0x772d20050ad4bd5b4a5b932adf668dbc677c69f6f090bed72d733b975c68b0324536a57b8af96f01ae442a2c8807147efb68bec1e4f25378d20dbdb3b747945b79bf38a3446f9b0879d640d340b6cb5019e9b5206688fedca4fcdb811b31847958f42976388fc850a2e42d29e97113010c36e8b04ea88d34180b9b4279c245aceb9b831474d62a24dfe3d0b728636c15cde0922c6c15e17e595435dcc5ade51a39f969faa4e84da36c720f4131c67f78ac0db16ebb0bdff7e6ab82a352fb59fadeeebcd02e9a0cc5ec3e531862d4701602dfac9485d642a5693dc4a7ae8ab6c869582317654953e9d7f543a0d8c4867dc79025d2641488cc9bde22923fa30039,
    0xf8d447b398476a516869f189c02cec2a67025720223b51f9b65038892838bfee87eeb4699965a017d6a82a8cc9ca133b9fe427fbdba4bc31e029880b510183a4cb4f6d335d7185f4c9f6d1fabd2158304a9184b79dc8062fb1abb63cd2d383d656a9e108d0a3b40fd2ff35cc62f15d7ce5891041c1bdf655753f7af31e398de7,
    0xcd7c7713a1a3cf924b145672d5265406e388fbf97774c8ebb0a81005203d5a69ccdea454c869a9519d6a7cd1f0733741fe7747129f8a0af7ce223029d8750d4aafcdd3ba7b1d5efe1658a47dbb2381cb8feade556bcf7d0dc04dc69fce0820607a9e2077489ac9963e01649a2bd2fc70d3f94895415badd3223ebe07afb4c775,
)
ROTATED = (
    0xbc1bb4a054afc2d3f13fdab970df7964d4501352871c55d678d629c29f9fb08523c61bfd953fb63c658d059b7a274ac3296c3db828c69a5c50e4fc8f45fa4c925cac34c86b1a1d88ff268483aa3a82a65ed289724bd51e8d76ffd845934ee82990316ffd97d0d25c5f364a5f9effd80119b54d98614d99b54b30700de57024c3a1985575e0ebaf8796742d586f95340b2c4f321964cf2d857cf6ab0efa18e3b16855c4489f182146b145a493d26bde79d0a04dd4c5a23eb695fddd5ee455a4cb4d5081b9804704356370d5432ee6ef7cf864781ee95ef3e339a1470016d74f44e801a83f23bc7e46efa8a5c8bd9a1cc6cf0c4948ea6f57eecb57de43d5128e9d,
    0x10001,
    0x549047f543dcf7c2fbd36eeaaab96c034e06e988d24084be33f86331a13cd2c827bae9de67f80a5ec0705333000925ecf2743c92cb91ecd6cf6f01b30f0c64dbef5963292e2cc156530de7c8a10048b7ae2d0ba91c3a24d007cd2056b6d064423b165e782c85adebe4c9ad49ab5aecaa21fa857599589f5f06fc3183adbf6a66da617d1e29d6ea414a226199b1b6210081d6e85efce4ae47ad22fd484839c2a554fc0349c81e7520ef6e780ce025e0100bbf0fba34f8552d055126849500d2d34a0bb227105b6f584a67fd9c0b2217680283193283a25daa58988e90aa6a11cd115ad9cb6a77f8d6497606cb1da6a24644402e977e90d0fd009f27601acb0201,
    0xe83d0fa2a6bcdeca388441a9c5790b86a7140f2e69d49a4df3614c4907647dc27b072853c14bf36428e6f979293ad70f8c1aed596902349ab13bb01871fbb5f13bdd9c546687e8455086a00ec13825154ff26d807ef5bcd78c286eab6ae7c2ffc6cb48aed08f2236363b5313dd6f1a2b99b42be534b81811a60c3903adcad541,
    0xcf5ac1799070812e144ee0c9117168fc562cf43041ba93f198432ac89c791cf83a439254a92c87fab7ec0ba57a28c5be3dc93582fc1e60eb5593b9de3baab9ef122b8e6d5e43d003a9525f92a5b121ccb23cba3200a38f640b4a2c966c33fb5c18051cf7bdb1b80be86ec3651f68ee05c4852fd4a8a978eb1f09b65292af965d,
)
SMALL = (
    0xd8ce403f7ce1be90aaa5043e2b646be6f12a992a248c1618d76cc279c892429373598a4fed6165853478a8b89fd532fcd562339e35c932ff5b90352cc2ef231f89123209c87235b55ee85c36d289b0b3d9bdfb0c94355e3d5b51fd3b3d0638bacb1642767831c7fb1f150ba0ce624f1d50f671309cb8e29687fa3303ea5b4c5f,
    0x10001,
    0x4e9d5b46aa2de13febe0b24d28c6dd297cc189907278297b10826b72657c438fdbffd7db6ba4297fc7ab08e30971f16f24a0db3ebf1a19d7e687c0005f1eff88c0f7b15fa0ca8910b0a9b0ad05af953175ed5642fcb331740e59997034ce3dcd175727cbd174dfb938ad948a5f009d7664a59ebfa4da192f5e38364c35932421,
    0xef2e20fc6cb96c210753aff9dfa47bcc19db3f4b2a21e95337c6e62d288de594cad08087b95b61d11765b44356a95ec5878f1f59cc101b55d7ae85cdedd70483,
    0xe80d526d78396f715587a5245ca1f463418ebf8b42cc1368bd92d93e72405d8a58a5f1d12c0565e210930f93b93009216d51bce7b5f7b2aeca7fc46e9bc929f5,
)
E3 = (
    0xaaa756988f72fe3d5f8ecbead36ad21d9eeaecae051a1224eb5e32b0b1933d31c38056fd20912f25392f60544c216a208c9e10b3e1182c8ff7ddaae64513413f6b3c49a9c1fc6529245157c38ec7ff11578b5a08cd035c0073af3f564bcbb333c83c3826e17f62c542eb6b21874f5b0fec81b3dc8d99609926e93f9c2af33eadb32bc9da552f5a9151a069abaa930252388aa53402264940cf007e962cc4afcf53056e6e1c4a04c3296a5b431ba280734b6cc99511a2e70acc9a411757af376d5f94bc6a6a7e0be9e1eb217111b6dd7620c7049e04069e95462c6bb3e3fc50830f28addf8320a7a97f4f61447c46ad2345738b6a74369fea61a36a9fbb46ae97,
    0x3,
    0x71c4e465b4f75428ea5f329c8cf1e169149c9dc958bc0c189ce97720766228cbd7aae4a8c060ca18d0ca403832c0f16b08696077eb65730aa53e71eed8b780d4f228311bd6a84370c2e0e52d09daaa0b8fb23c05de023d55a274d4e43287cccd3028256f40ff972e2c9cf2165a34e75ff30122930910eb10c49b7fbd71f77f1d5fee7430d47a3b128c500c9865f285cff9e2617b79f93c340ae47a8a7a624b0616e528b1b49113befba0330fd4d470caf5ffbb797232ea5e049b1782537d2fddf7530d46f646ed5d4f2d1b9cb5add0dccbeb705deb09951db88114081ca1b1eae2031c99a1d371e963762f755f819c4f4d8a97e68a293a1321b33094c5084e8b,
    0xe1c557278947ceeccc9555bb4614991e7d056e4d9cb208785166224eedcc947f953bedfd680ddba0fb5d6ea281eefbba09a633b247d0a683c86a579c9d6532cedee42733036f318b94da95371dde1a5d00c4e916c475955db565852f1f3db4094a0513adeee7a0821a894e9b3c63fce3eeb0f6878765b115b5bf7a54be3fa06f,
    0xc180c4698d303308b293010bcb92a07bc4b1a4ad2e7e667a6d43a4778764aac69b71c36625628b83b49ca008da74db88d0c6fcac9e85e0f9fd4746373d0e3cd18db4014cf5a47652564ce2cee35409cdee20f2fa5f02a98afc05487899cc1199721eef4b217bdc494f94cb7930a045c86272b1091d9317b7f957276bd57a9859,
)

SWITCHES = ("bad_nonce", "bad_aud", "bad_iss", "expired", "old_iat", "alg", "kid",
            "rotate_keys", "truncate_sig", "forge_e3", "small_key")

KIDS = {id(MAIN): "test-key-1", id(ROTATED): "test-key-2",
        id(SMALL): "test-key-small", id(E3): "test-key-e3"}

_SHA256_OID = bytes.fromhex("0609608648016503040201")


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _int_bytes(v):
    return v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big")


def b64u_int(v):
    return b64u(_int_bytes(v))


def _der(tag, body):
    n = len(body)
    if n < 0x80:
        length = bytes([n])
    else:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + body


def _der_int(v):
    raw = _int_bytes(v)
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _der(0x02, raw)


def digest_info_sha256(message):
    """DER DigestInfo for SHA-256, including the digest (the T of EMSA-PKCS1-v1_5)."""
    alg = _der(0x30, _SHA256_OID + _der(0x05, b""))
    return _der(0x30, alg + _der(0x04, hashlib.sha256(message).digest()))


def sign_rs256(key, message):
    n, e, d, p, q = key
    k = (n.bit_length() + 7) // 8
    tail = digest_info_sha256(message)
    em = b"\x00\x01" + b"\xff" * (k - 3 - len(tail)) + b"\x00" + tail
    m = int.from_bytes(em, "big")
    sp = pow(m % p, d % (p - 1), p)
    sq = pow(m % q, d % (q - 1), q)
    h = (pow(q, -1, p) * (sp - sq)) % p
    return (sq + h * q).to_bytes(k, "big")


def _icbrt(x):
    lo, hi = 0, 1 << ((x.bit_length() // 3) + 2)
    while lo < hi:
        mid = (lo + hi) // 2
        if mid ** 3 < x:
            lo = mid + 1
        else:
            hi = mid
    return lo


def forge_e3_signature(key, message):
    """Bleichenbacher-style forgery against an e=3 key: no private key used.

    The block is 00 01 FF 00 <DigestInfo+digest> <garbage>; a verifier that
    parses the padding and ignores what follows the digest accepts it."""
    n, e = key[0], key[1]
    assert e == 3
    k = (n.bit_length() + 7) // 8
    prefix = b"\x00\x01\xff\x00" + digest_info_sha256(message)
    room = 8 * (k - len(prefix))
    low = int.from_bytes(prefix, "big") << room
    s = _icbrt(low)  # smallest s with s**3 >= low
    assert s ** 3 < (int.from_bytes(prefix, "big") + 1) << room
    return s.to_bytes(k, "big")


def jwk(key, kid):
    return {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256",
            "n": b64u_int(key[0]), "e": b64u_int(key[1])}


def spki_der(key):
    """SubjectPublicKeyInfo DER of the public half: the usual HS256-confusion secret."""
    rsa_pub = _der(0x30, _der_int(key[0]) + _der_int(key[1]))
    alg = _der(0x30, bytes.fromhex("06092a864886f70d010101") + _der(0x05, b""))
    return _der(0x30, alg + _der(0x03, b"\x00" + rsa_pub))


def pem_private(key):
    """PKCS#1 private key in PEM, built at test time so no key block is committed."""
    n, e, d, p, q = key
    parts = [0, n, e, d, p, q, d % (p - 1), d % (q - 1), pow(q, -1, p)]
    der = _der(0x30, b"".join(_der_int(v) for v in parts))
    body = base64.b64encode(der).decode("ascii")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "\n".join(["-----BEGIN RSA PRIVATE KEY-----", *lines,
                      "-----END RSA PRIVATE KEY-----", ""])


class FakeProvider:
    """Discovery, JWKS and token endpoint behind an HTTP seam.

    `get_json(url)` and `post_form(url, data)` are the whole surface the flow
    code uses, so tests hand the provider to it as the HTTP client."""

    def __init__(self, issuer=ISSUER, client_id=CLIENT_ID, now=None, **switches):
        self.issuer = issuer
        self.client_id = client_id
        self.now = now or time.time
        self.requests = []
        self.codes = {}
        self._counter = 0
        for name in SWITCHES:
            setattr(self, "kid_override" if name == "kid" else name,
                    None if name in ("alg", "kid") else False)
        for name, value in switches.items():
            if name not in SWITCHES:
                raise TypeError("unknown switch " + name)
            setattr(self, "kid_override" if name == "kid" else name, value)

    # -- keys ---------------------------------------------------------
    @property
    def key(self):
        if self.small_key:
            return SMALL
        if self.forge_e3:
            return E3
        return ROTATED if self.rotate_keys else MAIN

    @property
    def key_id(self):
        return KIDS[id(self.key)]

    @property
    def kid(self):
        """The kid written into token headers (a switch can point it elsewhere)."""
        return self.kid_override or self.key_id

    def jwks(self):
        return {"keys": [jwk(self.key, self.key_id)]}

    # -- tokens -------------------------------------------------------
    def id_token(self, nonce, *, sub="user-1", drop=(), header=None, **claims):
        now = int(self.now())
        payload = {"iss": self.issuer, "aud": self.client_id, "sub": sub, "iat": now,
                   "exp": now + 3600, "nonce": nonce, "email": sub + "@example.test",
                   "email_verified": True}
        if self.bad_nonce:
            payload["nonce"] = "wrong-" + nonce
        if self.bad_aud:
            payload["aud"] = "someone-else"
        if self.bad_iss:
            payload["iss"] = self.issuer + "/evil"
        if self.expired:
            payload.update(iat=now - 300, exp=now - 200)
        if self.old_iat:
            payload.update(iat=now - 3600, exp=now + 3600)
        payload.update(claims)
        for name in drop:
            payload.pop(name, None)
        alg = self.alg or "RS256"
        head = {"alg": alg, "kid": self.kid, "typ": "JWT"}
        head.update(header or {})
        signing_input = (b64u(json.dumps(head).encode()) + "." +
                         b64u(json.dumps(payload).encode()))
        message = signing_input.encode("ascii")
        if alg == "none":
            sig = b""
        elif alg == "HS256":
            sig = hmac.new(spki_der(self.key), message, hashlib.sha256).digest()
        elif self.forge_e3:
            sig = forge_e3_signature(E3, message)
        else:
            sig = sign_rs256(self.key, message)
        if self.truncate_sig:
            sig = sig[:-1]
        return signing_input + "." + b64u(sig)

    # -- endpoints ----------------------------------------------------
    def discovery(self):
        return {"issuer": self.issuer,
                "authorization_endpoint": self.issuer + "/authorize",
                "token_endpoint": self.issuer + "/token",
                "jwks_uri": self.issuer + "/jwks"}

    def authorize(self, params):
        """What the user's browser would do: returns the authorization code."""
        self._counter += 1
        code = "code-%d" % self._counter
        self.codes[code] = dict(params)
        return code

    def get_json(self, url):
        self.requests.append(("GET", url))
        if url == self.issuer + "/.well-known/openid-configuration":
            return self.discovery()
        if url == self.issuer + "/jwks":
            return self.jwks()
        raise KeyError(url)

    def post_form(self, url, data):
        self.requests.append(("POST", url))
        if url != self.issuer + "/token":
            raise KeyError(url)
        info = self.codes.pop(data.get("code"), None)
        if info is None or data.get("grant_type") != "authorization_code":
            return {"error": "invalid_grant"}
        challenge = info.get("code_challenge")
        verifier = data.get("code_verifier", "")
        expected = b64u(hashlib.sha256(verifier.encode()).digest())
        if (info.get("code_challenge_method") != "S256" or not challenge
                or not hmac.compare_digest(challenge, expected)):
            return {"error": "invalid_grant"}
        if info.get("client_id", self.client_id) != data.get("client_id", self.client_id):
            return {"error": "invalid_grant"}
        return {"id_token": self.id_token(info.get("nonce", "")), "access_token": "fake-access",
                "token_type": "Bearer", "expires_in": 3600}
