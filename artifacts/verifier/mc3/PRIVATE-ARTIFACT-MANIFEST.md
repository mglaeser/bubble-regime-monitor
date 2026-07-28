# MC3 private-artifact manifest

The full ReviewSkeleton and ExecutableReviewPlan for each range carry
`publication_class: private`, so they are NOT committed — publishing them
would contradict their own classification, and at 7-8 MB of hash-dense
JSON they also exceed what the repository's secret gate can scan.

What IS committed is each artifact's publishable summary, its exact
SHA-256, and the exact command that regenerates it. The finalizer's
rebuild-before-network gate means a regenerated skeleton that differs by
one byte from the recorded digest is refused, so these digests are a
complete check on the private files.

Regenerate in a CLEAN CLONE. The working repository sets `diff.noprefix`
in repository-local git config, and hermetic planning refuses to run
under any local config that can reshape diff output.

PINs used (all twelve, exported before the commands below):
  VERIFIER_MAX_OUTPUT_TOKENS=8000
  VERIFIER_CONTEXT_MARGIN_TOKENS=4000
  VERIFIER_COST_CAP_MICRO_USD=50000000
  VERIFIER_REASONING_EFFORT_BY_MODEL=gpt-5.3-codex=medium,gpt-5.6-sol=medium,gpt-4.1-mini=
  VERIFIER_MAX_REVIEW_UNITS=5000
  VERIFIER_MAX_GENERATION_CALLS=5000
  VERIFIER_MAX_COUNT_CALLS=100000
  VERIFIER_COUNT_TIMEOUT_SECONDS=30
  VERIFIER_COUNT_MAX_RETRIES=2
  VERIFIER_GENERATION_TIMEOUT_SECONDS=120
  VERIFIER_GENERATION_MAX_RETRIES=1
  VERIFIER_TOKEN_DRIFT_TOLERANCE=64

## A-pr25
  range                    75a093de45f73169072837c7c062fab421caaf8b -> b08844a0755710035d62830faa84902d9d85d3fe
  skeleton file sha256     d9beba1a2d7aff01a4021f330b2a7db0e3b83a1483af57e5d0d3a923a760ba33
  review_skeleton_sha256   d76fb425791b3512603012514e76a446deb1bd700f54149d3df58021a4ad9fbf
  plan file sha256         906b45b89627fe12d90c5718dd528955e3ff6571374582c896973b9e1f3e04ab
  executable_plan_sha256   09eb84d55e14d5e915146112b81c9ad98e93551e4abd0e55593ff1130ec8f614
  disposition_root         d963b6d4c9f2ae081fdca94106d85dd31f3656ed1919ec4a4bc0742e82b71c4c

  python scripts/independent_verify.py --plan --base 75a093de45f73169072837c7c062fab421caaf8b \
      --head b08844a0755710035d62830faa84902d9d85d3fe \
      --output A-pr25-skeleton.json --public-output A-pr25-public.json
  python scripts/independent_verify.py --finalize \
      --skeleton A-pr25-skeleton.json --output A-pr25-plan.json \
      --public-output A-pr25-plan-public.json \
      --allowlist artifacts/verifier/mc3/A-pr25-allowlist.txt

## B-precursor
  range                    b08844a0755710035d62830faa84902d9d85d3fe -> b15c5a77759938a2149a46bb1998ad81ec182556
  skeleton file sha256     e99e629864a70abb9b6f8afacc17c09abfd21b7fcc3ce0c2d637bd0a0c164cdf
  review_skeleton_sha256   3a070571f875134475e54984fa2ec6e309ff0821f6cda5fc7bbdcb1eeb9064da
  plan file sha256         12c4438713086e5d8b5991de9216d14b55052f0603f2e00fecc5c487efdcf850
  executable_plan_sha256   03d555ba5e92376b61aea2b6e08dbb6ddc909efd794ffb0f56ebd7dd7042405f
  disposition_root         1cbdaff65515acbff8ab971240d6b27f86a1c755f82d4cafc1fade2ece20b215

  python scripts/independent_verify.py --plan --base b08844a0755710035d62830faa84902d9d85d3fe \
      --head b15c5a77759938a2149a46bb1998ad81ec182556 \
      --output B-precursor-skeleton.json --public-output B-precursor-public.json
  python scripts/independent_verify.py --finalize \
      --skeleton B-precursor-skeleton.json --output B-precursor-plan.json \
      --public-output B-precursor-plan-public.json \
      --allowlist artifacts/verifier/mc3/B-precursor-allowlist.txt

## C-pr23
  range                    b08844a0755710035d62830faa84902d9d85d3fe -> a9062aa656a5a6f3dbe5991d16ce9c218aad0454
  skeleton file sha256     a48ddaa9e95d39aee76ec87950acb03ba3340f79e9801bca4d96ad9e7146732a
  review_skeleton_sha256   d6a3466c76906bd5f8928e605d799b49e6e95d471f35fa8508975adc9edb6dc7
  plan file sha256         0d231a076ede23b8930f3c6b205943ef2f74e81552c59fa61703a529bedaaa50
  executable_plan_sha256   5eeb5d7985d269363e4f6c534f21e60eb17bd43c48c58fff37cab01aed8b146b
  disposition_root         320e15d4a4dc1884303da1f209b3c7029b3cdad8e1e219f47eacee606daccf9b

  python scripts/independent_verify.py --plan --base b08844a0755710035d62830faa84902d9d85d3fe \
      --head a9062aa656a5a6f3dbe5991d16ce9c218aad0454 \
      --output C-pr23-skeleton.json --public-output C-pr23-public.json
  python scripts/independent_verify.py --finalize \
      --skeleton C-pr23-skeleton.json --output C-pr23-plan.json \
      --public-output C-pr23-plan-public.json \
      --allowlist artifacts/verifier/mc3/C-pr23-allowlist.txt
