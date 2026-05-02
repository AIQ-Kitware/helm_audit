import Mathlib

noncomputable section

open scoped BigOperators

namespace WikiFactNoise

/-!
# WikiFact stochastic-recipe scaffold

This file is intended to support the claim in the HELM reproducibility
writeup that WikiFact's ~0.92 instance-level agreement is compatible with
recipe-level stochasticity.

The core mathematical model is deliberately small:

* A prompt has a per-run binary success/failure outcome for `exact_match@5`.
* `p i` is the probability that prompt `i` succeeds in one full run.
* Two independent runs agree on prompt `i` with probability
  `p i ^ 2 + (1 - p i) ^ 2`.
* A uniform Bernoulli model using only the average hit rate gives a lower
  bound. Prompt-dependent difficulty adds a nonnegative heterogeneity term.

For the `num_outputs = 5` recipe, if `q i` is a single-sample exact-match
probability, then the per-run `exact_match@5` probability is
`1 - (1 - q i)^5`, under the usual conditional-independence idealization.
-/

/-- Probability that at least one of `k` independent samples hits, given
single-sample hit probability `q`. -/
def hitAtK (k : ℕ) (q : ℝ) : ℝ :=
  1 - (1 - q)^k

/-- The WikiFact `num_outputs = 5` case. -/
def hitAt5 (q : ℝ) : ℝ :=
  hitAtK 5 q

/-- Agreement probability for two independent binary runs with hit probability `p`. -/
def agree (p : ℝ) : ℝ :=
  p^2 + (1 - p)^2

/-- Agreement probability after converting one-sample hit probability to exact_match@5. -/
def top5Agree (q : ℝ) : ℝ :=
  agree (hitAt5 q)

/-- Bernoulli variance term. -/
def bernVar (p : ℝ) : ℝ :=
  p * (1 - p)

/-- Mean of a finite benchmark-indexed quantity. -/
def avg {ι : Type} [Fintype ι] (x : ι → ℝ) : ℝ :=
  (∑ i, x i) / (Fintype.card ι : ℝ)

/-- Mean expected agreement under heterogeneous per-prompt hit probabilities. -/
def expectedAgreement {ι : Type} [Fintype ι] (p : ι → ℝ) : ℝ :=
  avg (fun i => agree (p i))

/-- Uniform Bernoulli agreement prediction at the average hit rate. -/
def uniformAgreement {ι : Type} [Fintype ι] (p : ι → ℝ) : ℝ :=
  agree (avg p)

/-- Population variance of per-prompt hit probabilities. -/
def promptVariance {ι : Type} [Fintype ι] (p : ι → ℝ) : ℝ :=
  avg (fun i => (p i - avg p)^2)

/-- Predicted variance of the difference between two aggregate benchmark scores. -/
def scoreDiffVar {ι : Type} [Fintype ι] (p : ι → ℝ) : ℝ :=
  2 * (∑ i, bernVar (p i)) / (Fintype.card ι : ℝ)^2

/-- Same variance prediction, parameterized by one-sample probabilities `q i`. -/
def scoreDiffVarTop5 {ι : Type} [Fintype ι] (q : ι → ℝ) : ℝ :=
  scoreDiffVar (fun i => hitAt5 (q i))

/-- Binary agreement is one minus binary disagreement. -/
theorem agree_eq_one_minus_disagree (p : ℝ) :
    agree p = 1 - 2 * p * (1 - p) := by
  unfold agree; ring

/-- Agreement is minimized at `p = 1/2` and grows as `p` moves toward `0` or `1`. -/
theorem agree_centered (p : ℝ) :
    agree p = 2 * (p - 1 / 2)^2 + 1 / 2 := by
  unfold agree; ring

/-- Expands the top-5 agreement probability in terms of the one-sample hit rate. -/
theorem top5_agree_expand (q : ℝ) :
    top5Agree q = (1 - (1 - q)^5)^2 + (1 - q)^10 := by
  unfold top5Agree agree hitAt5 hitAtK; ring

/-- The homogeneous `p = 0.85` agreement value is `0.745`. -/
example : agree ((85 : ℝ) / 100) = (149 : ℝ) / 200 := by
  norm_num [agree]

/-- The homogeneous `p = 0.90` agreement value is `0.82`. -/
example : agree ((90 : ℝ) / 100) = (41 : ℝ) / 50 := by
  norm_num [agree]

/--
If the homogeneous exact_match@5 hit rate is between `0.85` and `0.90`,
then the homogeneous agreement prediction is between `0.745` and `0.82`.
-/
theorem agree_range_85_90
    (p : ℝ)
    (hlo : (85 : ℝ) / 100 ≤ p)
    (hhi : p ≤ (90 : ℝ) / 100) :
    (149 : ℝ) / 200 ≤ agree p ∧ agree p ≤ (41 : ℝ) / 50 := by
  unfold agree; constructor <;> nlinarith [sq_nonneg (p - 85/100), sq_nonneg (p - 90/100)]

/-- So homogeneous `p ∈ [0.85,0.90]` cannot predict agreement `0.92`. -/
theorem not_agree_92_on_85_90
    (p : ℝ)
    (hlo : (85 : ℝ) / 100 ≤ p)
    (hhi : p ≤ (90 : ℝ) / 100) :
    agree p ≠ (23 : ℝ) / 25 := by
  unfold agree; nlinarith [sq_nonneg (p - 90/100)]

/-!
## Two-prompt heterogeneity

These lemmas are easy to prove and are useful for exposition. They show the
mechanism before the fully finite-indexed theorem below.
-/

/-- For two prompts, heterogeneity adds exactly `(p - q)^2 / 2` agreement. -/
theorem two_prompt_heterogeneity (p q : ℝ) :
    (agree p + agree q) / 2 =
      agree ((p + q) / 2) + ((p - q)^2) / 2 := by
  unfold agree; ring

/-- Therefore, heterogeneity cannot lower expected agreement. -/
theorem two_prompt_heterogeneity_ge (p q : ℝ) :
    agree ((p + q) / 2) ≤ (agree p + agree q) / 2 := by
  rw [two_prompt_heterogeneity]; nlinarith [sq_nonneg (p - q)]

/-- The top-5 version of the two-prompt heterogeneity identity. -/
theorem two_prompt_top5_heterogeneity (q₁ q₂ : ℝ) :
    (top5Agree q₁ + top5Agree q₂) / 2 =
      agree ((hitAt5 q₁ + hitAt5 q₂) / 2)
        + ((hitAt5 q₁ - hitAt5 q₂)^2) / 2 := by
  unfold top5Agree; exact two_prompt_heterogeneity (hitAt5 q₁) (hitAt5 q₂)

/-!
## Finite-benchmark heterogeneity scaffold

The theorem below is the central claim to prove for the paper:

`expectedAgreement p = uniformAgreement p + 2 * promptVariance p`.

This directly implies:

`uniformAgreement p ≤ expectedAgreement p`.

That is the precise version of: the uniform Bernoulli prediction is a lower
bound/floor, and trained-model prompt-dependent difficulty can only raise
expected agreement above that floor.

The proof below uses the standard finite-sum decomposition: center the
per-prompt hit rates around their mean, expand agreement pointwise, sum over
prompts, and use the fact that the centered linear term sums to zero.
-/

/-
Variance decomposition for heterogeneous Bernoulli agreement.
-/
theorem expectedAgreement_eq_uniformAgreement_add_variance
    {ι : Type} [Fintype ι] [Nonempty ι]
    (p : ι → ℝ) :
    expectedAgreement p = uniformAgreement p + 2 * promptVariance p := by
  unfold expectedAgreement uniformAgreement promptVariance avg agree
  ring_nf
  norm_num [Finset.sum_add_distrib, Finset.mul_sum _ _ _, Finset.sum_mul _ _ _]
  ring_nf
  norm_num [← Finset.mul_sum _ _ _, ← Finset.sum_mul]
  ring_nf
  by_cases h : Fintype.card ι = 0 <;> simp_all +decide [pow_three, sq, mul_assoc]

/-- The uniform Bernoulli prediction is a lower bound/floor. -/
theorem uniformAgreement_le_expectedAgreement
    {ι : Type} [Fintype ι] [Nonempty ι]
    (p : ι → ℝ) :
    uniformAgreement p ≤ expectedAgreement p := by
  rw [expectedAgreement_eq_uniformAgreement_add_variance]
  have : 0 ≤ promptVariance p := by
    unfold promptVariance avg
    positivity
  linarith

/-- Exact_match@5 version of the lower-bound/floor statement. -/
theorem uniformAgreement_le_expectedAgreement_top5
    {ι : Type} [Fintype ι] [Nonempty ι]
    (q : ι → ℝ) :
    uniformAgreement (fun i => hitAt5 (q i)) ≤
      expectedAgreement (fun i => hitAt5 (q i)) :=
  uniformAgreement_le_expectedAgreement _

/-!
## Benchmark-score difference scaffold

This is a separate but related claim: if two full benchmark scores are
independent sums of promptwise Bernoulli outcomes, their expected difference is
zero, and the variance of the difference is

`2 / N^2 * ∑ i, p_i * (1 - p_i)`.

The algebraic quantity below is ready. A future probability-space layer can
connect it to random variables formally.
-/

/-- The algebraic variance formula used for two independent benchmark runs. -/
theorem scoreDiffVar_formula {ι : Type} [Fintype ι] (p : ι → ℝ) :
    scoreDiffVar p =
      2 * (∑ i, p i * (1 - p i)) / (Fintype.card ι : ℝ)^2 := by
  simp [scoreDiffVar, bernVar]

/-- The same benchmark-score variance formula for exact_match@5. -/
theorem scoreDiffVarTop5_formula {ι : Type} [Fintype ι] (q : ι → ℝ) :
    scoreDiffVarTop5 q =
      2 * (∑ i, hitAt5 (q i) * (1 - hitAt5 (q i))) /
        (Fintype.card ι : ℝ)^2 := by
  simp [scoreDiffVarTop5, scoreDiffVar, bernVar]

/-!
## Paper-facing theorem map

The paper sentence can be tied to these theorem names:

1. "WikiFact uses stochastic exact_match@5."
   * Model with `hitAt5` and `top5Agree`.
   * The algebra is `top5_agree_expand`.

2. "For a binary metric, a uniform Bernoulli model gives agreement
   `p^2 + (1-p)^2`."
   * Definition: `agree`.
   * Rewrite: `agree_eq_one_minus_disagree`.

3. "The uniform Bernoulli agreement is a lower-bound floor."
   * Main theorem: `uniformAgreement_le_expectedAgreement`.
   * Exact decomposition: `expectedAgreement_eq_uniformAgreement_add_variance`.

4. "Prompt-dependent difficulty explains why trained models sit above the
   uniform floor."
   * Two-prompt identity: `two_prompt_heterogeneity`.
   * Finite identity: `expectedAgreement_eq_uniformAgreement_add_variance`.

5. "Two independent stochastic benchmark scores can differ naturally."
   * Algebraic variance quantity: `scoreDiffVar_formula`.
-/

end WikiFactNoise
