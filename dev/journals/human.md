### 2026-04-06

I'm noticing that HELM favors external providers like together.ai. They talk a bit in their paper about limitations of model providers, and how they evaluate the streams that the users produce and are not directly comparing the model weights, but I don't see evidence that direct comparison of model weights is out of scope. They only mention standardized conditions.

Here I think I want to build something that works towards providing more insight into the differences between providers and hardware and works to build a distribution over evaluations, rather than pointwise results. That is computationally expensive to run benchmarks multiple times, so we should be careful to avoid structing this in the naive way. Perhaps by running certain subsets multiple times we can start to estimate distributions from pointwise or partial distributions of results.   


I'm also looking into VLLM for running these local results, which HELM seems to support on some level.

A challenge is going to be that VLLM doesn't seem to support switching models,
you load one up at init time. So when running benchmarks we can only run
batches of the same model. And then we need to switch out models.



Would this repo be better if we converted the HELM results to every-eval-ever and then performed the aggregation logic over that format? 
