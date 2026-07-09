-- ─────────────────────────────────────────────────────────────────────────────
-- Migration: explicit cost_contracts.contract_type
--
-- Adds a stored commercial-model discriminator (payg | committed | prepaid) so
-- reporting/metering and GRC read the model from a column instead of inferring
-- it from (minimum_amount_usd, bill_commitment_true_up, credit_purchases
-- presence). Follows the existing text+CHECK convention used for billing_period,
-- period_anchor, and status (NOT a native pg enum, which is painful to ALTER).
--
-- Backfill note: both live contracts (exa-search, mags-anthropic-llm) carry a
-- real prepaid seed in credit_purchases (usd_paid 9.39 / 39.28), so they classify
-- as 'prepaid'. The commitment arm is kept for any older committed-style row that
-- put its ceiling in minimum_amount_usd (the wizard never wrote commitment_credits).
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

ALTER TABLE cost_contracts
  ADD COLUMN IF NOT EXISTS contract_type text NOT NULL DEFAULT 'payg';

ALTER TABLE cost_contracts
  DROP CONSTRAINT IF EXISTS cost_contracts_contract_type_check;

ALTER TABLE cost_contracts
  ADD CONSTRAINT cost_contracts_contract_type_check
  CHECK (contract_type IN ('payg', 'committed', 'prepaid'));

-- Backfill existing rows by inference (the old implicit model), so history keeps
-- an accurate stated type rather than silently defaulting to 'payg'.
UPDATE cost_contracts c SET contract_type =
  CASE
    WHEN EXISTS (SELECT 1 FROM credit_purchases p WHERE p.contract_id = c.id) THEN 'prepaid'
    WHEN COALESCE(c.commitment_credits, 0) > 0 THEN 'committed'
    WHEN COALESCE(c.minimum_amount_usd, 0) > 0 THEN 'committed'
    ELSE 'payg'
  END;

COMMIT;

-- Verify:
--   SELECT vendor_name, contract_type, minimum_amount_usd, bill_commitment_true_up,
--          (SELECT count(*) FROM credit_purchases p WHERE p.contract_id = c.id) AS purchases
--   FROM cost_contracts c ORDER BY created_at;
-- Expect: exa-search_Contract -> prepaid, mags-anthropic-llm_Contract -> prepaid.
