---
name: python-performance-observability
description: Profile and optimize Python bot and scraper performance, concurrency, memory, throughput, latency, structured logs, metrics, and tracing.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: operations
---

# Python Performance and Observability

## Measure before optimizing
Establish workload and baseline: request rate, job volume, p50/p95/p99 latency, throughput, error rate, memory, CPU, DB pool saturation, and external API latency. Use `cProfile`, `pyinstrument`, `tracemalloc`, or benchmark tools appropriate to the symptom. Do not optimize based on intuition alone.

## Async throughput
- Bound concurrent tasks with semaphores/queues and per-host/per-provider limits.
- Apply backpressure when producers outpace consumers.
- Use connection pooling and reuse clients.
- Move CPU-bound work to process pools or dedicated workers when profiling justifies it.
- Move blocking I/O off the event loop.
- Respect cancellation and propagate deadlines.
- Avoid unbounded in-memory queues and result accumulation.

## Scraper-specific metrics
Track requests by host/status, latency, retries, throttling, bytes, parse success, records per page, rejected records, duplicate rate, last successful run, and selector/schema drift.

## Bot-specific metrics
Track update throughput, handler duration, platform API errors/rate limits, queue depth/age, webhook response latency, duplicate events, FSM persistence failures, and worker lag. Avoid high-cardinality labels such as raw user/chat IDs.

## Logging and tracing
- Emit structured logs with timestamp, level, service, environment, operation, correlation ID, and safe error context.
- Redact tokens, cookies, auth headers, message contents, and personal data by default.
- Use metrics for trends and traces for cross-service latency; do not use logs as a substitute for metrics.
- Add health/readiness checks that reflect actual dependencies without leaking details.
- Define alert thresholds and runbooks for actionable failure conditions.

## Optimization sequence
1. Reproduce representative workload.
2. Profile CPU, memory, I/O, and DB.
3. Identify the dominant bottleneck.
4. Form one optimization hypothesis.
5. Change one thing.
6. Benchmark before/after under comparable conditions.
7. Check correctness, fairness, rate limits, and operational complexity.
8. Keep the optimization only when measured benefit justifies cost.

## Reliability
Set deadlines and resource budgets per job. Use bounded retries, circuit breakers only when justified, graceful degradation, and load shedding. Avoid retry amplification during provider outages.

## Report
Include workload, environment, baseline, bottleneck evidence, change, before/after measurements, correctness checks, and remaining constraints.
