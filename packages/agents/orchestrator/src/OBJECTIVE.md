# Agent: Orchestrator

## Objective

The Orchestrator is the control plane for the entire multi-agent pipeline. It manages the sequential flow of data between the Searcher, Researcher, and Analyst agents — either as a direct coordination layer or via a Step Functions / Lambda architecture for decoupled, scalable execution.

## Responsibilities

- Define and execute the end-to-end pipeline: Searcher -> Researcher -> Analyst
- Route data between agents, ensuring each stage receives the correct inputs from the previous stage
- Manage pipeline configuration (which tickers to track, search keywords, analysis scope, report format)
- Handle error recovery and retries when individual agent stages fail
- Support both synchronous (direct invocation) and asynchronous (Step Functions / Lambda) execution modes
- Trigger pipelines on schedule or on-demand

## Execution Modes

### Direct Orchestration
- The Orchestrator invokes each agent in sequence within a single process
- Suitable for development, testing, and low-latency use cases

### Step Functions / Lambda Architecture
- Each agent runs as an independent Lambda function
- The Orchestrator defines the state machine that connects them
- Enables independent scaling, retry policies, and parallel fan-out (e.g., multiple Searcher invocations for different tickers)
- Pipeline state is managed externally (Step Functions state, SQS, or S3 intermediate storage)

## Inputs

- Pipeline trigger (scheduled cron, manual invocation, or event-driven)
- Configuration: target tickers, keywords, data sources, report parameters
- Execution mode selection (direct vs. Step Functions)

## Outputs

- Final report from the Analyst agent, delivered to the configured destination
- Pipeline execution metadata (timing, stage results, errors)
- Intermediate artifacts stored for auditability and debugging

## Key Considerations

- The Orchestrator should be stateless per execution — all pipeline state lives in the message payloads passed between stages
- Agent interfaces should be well-defined contracts (Pydantic models in the core package) so agents can evolve independently
- Support partial pipeline runs (e.g., re-run Analyst with cached Researcher output)
- Logging and observability at each stage transition for debugging and monitoring
