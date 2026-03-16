---
name: alpha-report
description: >
  Generate a structured investment alpha report. Use after completing analysis
  (manually or via analyze-market) to produce an actionable report that highlights
  what the market doesn't know yet.
---

# Alpha Report

Generate a "What the market doesn't know yet" investment report.

## Input Required

- **Signal**: the contrarian or cross-market insight
- **Supporting data**: price data, fundamentals, macro context, sentiment
- **Conviction score**: 1-10 rating from analysis
- **Time horizon**: short / medium / long

## Report Structure

### 1. Executive Summary (3 sentences max)
- What is the signal?
- Why does the market have it wrong?
- What is the expected outcome and timeline?

### 2. Thesis
- Core argument in 2-3 paragraphs
- Must answer: "Why is this not priced in?"

### 3. Evidence Chain
- List each piece of supporting evidence with source and date
- Show the causal chain: A → B → C → investment conclusion
- Cross-market links: explicitly map which market/data leads to which conclusion

### 4. Consensus View
- What the market currently believes (analyst ratings, sentiment, positioning)
- Why the consensus formed (narrative, recent events)
- Where the consensus is vulnerable

### 5. Contrarian Angle
- Specific point of disagreement with consensus
- What data the market is ignoring or misinterpreting
- Historical analogues if available

### 6. Risk Assessment
- Top 3 risks that invalidate the thesis
- For each risk: probability estimate, impact, and what to monitor
- Kill condition: "Exit if X happens"

### 7. Action
- Suggested direction: long / short / pair trade / avoid
- Time horizon and key milestones
- Position sizing guidance: high conviction (full size) / medium (half) / speculative (small)

## Formatting Rules

- Use data tables for comparisons.
- Cite every data point: source + date.
- Bold the contrarian angle - this is the core value.
- Flag uncertainty explicitly. Never present as certainty.
- Include a "Last updated" timestamp.

## Output Template

```markdown
# Alpha Report: {Title}
> Generated: {date} | Horizon: {horizon} | Conviction: {score}/10

## Executive Summary
{3 sentences}

## Thesis
{2-3 paragraphs}

## Evidence Chain
1. {Evidence} — Source: {source}, {date}
2. {Evidence} — Source: {source}, {date}
   → leads to: {conclusion}

## Consensus View
- Analyst consensus: {bullish/bearish/neutral} ({detail})
- News sentiment: {score}
- Institutional positioning: {detail}

## **Contrarian Angle**
**{The key disagreement with market consensus}**
{Explanation}

## Risk Assessment
| Risk | Probability | Impact | Monitor |
|------|-------------|--------|---------|
| {risk 1} | {low/med/high} | {description} | {indicator} |

**Kill condition**: {exit criteria}

## Action
- Direction: {long/short/pair/avoid}
- Horizon: {timeframe}
- Sizing: {full/half/speculative}
- Key milestones: {what to watch}
```

## Notes

- This skill can be invoked standalone or as Step 7 of `/analyze-market`.
- Quality of output depends entirely on quality of input data. Garbage in, garbage out.
- Always disclaim: this is analysis, not financial advice.
