-- session gains a bounded window for revealing secrets, closed whenever its user's access changes.

ALTER TABLE session ADD COLUMN reveal_until TEXT CHECK (
    reveal_until IS NULL OR reveal_until GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
);

ALTER TABLE session ADD COLUMN reveal_count INTEGER NOT NULL DEFAULT 0 CHECK (reveal_count BETWEEN 0 AND 20);

CREATE TRIGGER user_closes_reveal_windows
AFTER UPDATE OF password_hash, role, is_enabled ON user
WHEN OLD.password_hash IS NOT NEW.password_hash
  OR OLD.role IS NOT NEW.role
  OR OLD.is_enabled IS NOT NEW.is_enabled
BEGIN
    UPDATE session SET reveal_until = NULL, reveal_count = 0 WHERE user_id = NEW.id;
END;
