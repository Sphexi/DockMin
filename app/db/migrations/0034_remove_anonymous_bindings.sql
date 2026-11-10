-- A binding with no credential set recorded an anonymous pull. An image with no binding is
-- checked and pulled anonymously, so those rows carry nothing.

DELETE FROM image_credential_binding
WHERE state = 'bound' AND registry_credential_id IS NULL;
