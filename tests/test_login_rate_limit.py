"""PR 4.4: login endpoint rate limiting (token bucket + escalating lockout).

On main, `POST /auth/login` had no throttling at all — unlimited password
guesses. Now: 5 attempts per rolling 60 s per client IP, and consecutive
wrong passwords escalate into a lockout (429 + `Retry-After`).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from mcp_proxy.api.auth import router as auth_router
from mcp_proxy.rate_limit import LoginRateLimiter
from mcp_proxy.settings import Settings


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def make_client(limiter: LoginRateLimiter) -> TestClient:
    app = FastAPI()
    app.state.settings = Settings(
        admin_password="right-pw", session_secret="s3cret-s3cret-1234"
    )
    app.state.login_rate_limiter = limiter
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_router)
    return TestClient(app)


# -- unit ---------------------------------------------------------------------


def test_bucket_allows_five_then_blocks_until_window_passes() -> None:
    clock = FakeClock()
    lim = LoginRateLimiter(clock=clock)
    assert [lim.hit("ip") for _ in range(5)] == [None] * 5
    blocked = lim.hit("ip")
    assert blocked is not None and 0 < blocked <= 60
    clock.advance(61)
    assert lim.hit("ip") is None


def test_rejected_attempts_do_not_extend_the_bucket() -> None:
    clock = FakeClock()
    lim = LoginRateLimiter(clock=clock)
    for _ in range(5):
        assert lim.hit("ip") is None
    clock.advance(30)
    assert lim.hit("ip") is not None  # rejected mid-window
    clock.advance(31)  # window from the 5 *recorded* attempts now passed
    assert lim.hit("ip") is None


def test_failure_streak_locks_out_with_escalation() -> None:
    clock = FakeClock()
    lim = LoginRateLimiter(clock=clock)
    for _ in range(5):
        lim.record_failure("ip")
    blocked = lim.hit("ip")
    assert blocked is not None and blocked <= 60
    clock.advance(61)
    assert lim.hit("ip") is None
    for _ in range(5):  # streak 6..10 → doubling lockouts
        lim.record_failure("ip")
    clock.advance(61)
    blocked2 = lim.hit("ip")  # streak 10 → lockout 60 * 2**5 = 1920 s, 61 elapsed
    assert blocked2 is not None and 1855 <= blocked2 <= 1920


def test_success_clears_state() -> None:
    clock = FakeClock()
    lim = LoginRateLimiter(clock=clock)
    for _ in range(3):
        lim.record_failure("ip")
    lim.record_success("ip")
    for _ in range(4):  # streak restarted → still below lockout threshold
        lim.record_failure("ip")
    assert lim.hit("ip") is None
    lim.record_failure("ip")  # streak hits 5 → locked
    assert lim.hit("ip") is not None


# -- integration ----------------------------------------------------------------

WRONG = {"password": "wrong"}


def test_login_four_oh_one_then_four_two_nine() -> None:
    client = make_client(LoginRateLimiter())
    for _ in range(5):
        r = client.post("/auth/login", json=WRONG)
        assert r.status_code == 401
    r = client.post("/auth/login", json=WRONG)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    assert "Too many login attempts" in r.json()["detail"]


def test_lockout_then_correct_password_succeeds() -> None:
    clock = FakeClock()
    client = make_client(LoginRateLimiter(clock=clock))
    for _ in range(5):
        assert client.post("/auth/login", json=WRONG).status_code == 401
    assert client.post("/auth/login", json=WRONG).status_code == 429
    clock.advance(61)
    ok = client.post("/auth/login", json={"password": "right-pw"})
    assert ok.status_code == 200
    me = client.get("/auth/me")
    assert me.json()["logged_in"] is True


def test_correct_password_resets_bucket() -> None:
    client = make_client(LoginRateLimiter())
    for _ in range(4):
        assert client.post("/auth/login", json=WRONG).status_code == 401
    assert client.post("/auth/login", json={"password": "right-pw"}).status_code == 200
    # Fresh state: five more wrong ones are tolerated again (401, not 429).
    for _ in range(5):
        assert client.post("/auth/login", json=WRONG).status_code == 401


def test_auth_disabled_bypasses_limiter() -> None:
    app = FastAPI()
    app.state.settings = Settings()
    app.state.login_rate_limiter = LoginRateLimiter(max_attempts=0)
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_router)
    client = TestClient(app)
    r = client.post("/auth/login", json=WRONG)
    assert r.status_code == 200
