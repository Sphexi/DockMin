-- user gains a link to an access platform identity, and session records how it was opened.

ALTER TABLE user ADD COLUMN external_identity TEXT COLLATE NOCASE CHECK (
    external_identity IS NULL OR length(external_identity) BETWEEN 1 AND 320
);

CREATE UNIQUE INDEX idx_user_external_identity ON user (external_identity)
    WHERE external_identity IS NOT NULL;

ALTER TABLE session ADD COLUMN sign_in_method TEXT NOT NULL DEFAULT 'password' CHECK (
    sign_in_method IN ('password', 'jwt')
);
