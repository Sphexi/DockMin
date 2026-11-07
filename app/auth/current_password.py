"""Re-entry of the signed-in user's password, throttled with the same lockout as sign-in."""

from functools import partial

from app.auth.errors import ValidationFailed
from app.auth.passwords import HashingBusy, PasswordHasher
from app.auth.throttle import LoginThrottleService
from app.db.engine import retry_busy
from app.db.repositories.user import User
from app.services.activity import current_audit


async def verify_current_password(
    user: User,
    password: str,
    remote_address: str,
    *,
    throttle: LoginThrottleService,
    hasher: PasswordHasher,
    field_name: str = "current_password",
) -> None:
    """Raise ValidationFailed on `field_name` unless the password is the user's, counting a miss.

    Runs inside an audit scope and sets its reason to `locked_out` or `bad_current_password`.
    """
    async with throttle.guard(user.username, remote_address):
        audit = current_audit()
        retry_after = await throttle.retry_after(user.username, remote_address)
        if retry_after > 0:
            audit.reason = "locked_out"
            raise ValidationFailed({field_name: "Too many failed attempts. Try again later."})
        # Counted before the check, so an attempt cancelled mid-verification has still cost one.
        await throttle.record_failure(user.username, remote_address)
        try:
            verified = await hasher.verify(user.password_hash, password)
        except HashingBusy:
            await retry_busy(partial(throttle.refund, user.username, remote_address))
            raise
        if not verified:
            audit.reason = "bad_current_password"
            raise ValidationFailed({field_name: "The current password is incorrect."})
        await retry_busy(partial(throttle.settle_success, user.username, remote_address))
