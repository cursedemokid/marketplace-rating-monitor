-- Store the classifier version and configured batch size on each paid request.
-- Other AI uses (such as candidate naming) leave these fields empty.
alter table wb.ai_cost_requests
    add column classifier_prompt_sha256 text,
    add column classifier_batch_size integer,
    add constraint ai_classifier_prompt_sha256_check
        check (classifier_prompt_sha256 is null or classifier_prompt_sha256 ~ '^[0-9a-f]{64}$'),
    add constraint ai_classifier_batch_size_check
        check (classifier_batch_size is null or classifier_batch_size > 0);
