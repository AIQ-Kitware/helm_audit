
OFFICIAL_RUN="/data/crfm-helm-public/classic/benchmark_output/runs/v0.3.0/mmlu:subject=us_foreign_policy,method=multiple_choice_joint,model=eleutherai_pythia-6.9b,data_augmentation=canonical"
LOCAL_RUN="/data/crfm-helm-audit/audit-smoke-apples/helm/helm_id_10qfn238081w/benchmark_output/runs/audit-smoke-apples/mmlu:subject=us_foreign_policy,method=multiple_choice_joint,model=eleutherai_pythia-6.9b,data_augmentation=canonical"

every_eval_ever convert helm \
  --log-path "$OFFICIAL_RUN" \
  --output-dir "$HOME/data/eee-poc/mmlu-usfp-pythia/official-usfp-pythia-v0.3.0" \
  --source-organization-name "PUT_OFFICIAL_ORG_HERE" \
  --evaluator-relationship third_party \
  --eval-library-name "HELM" \
  --eval-library-version "v0.3.0"
