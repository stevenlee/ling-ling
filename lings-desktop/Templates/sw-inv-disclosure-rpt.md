### Output Format (Software & Algorithm Invention Disclosure)

Begin your response with a YAML frontmatter block matching the schema below. **Emit it once, at the very start of your response — never reproduce it inside the body.**

```yaml
---
title: "Invention Disclosure: (Topic)"
tags: ["patent", "disclosure", "software"]
type: "disclosure"
---
```

After the closing `---`, the Markdown body:

# Invention Disclosure (Software & Algorithm)
> **Purpose:**  _For inventors to complete at the early drafting stage and hand off to a patent engineer for drafting the specification and claims._
> 
> **How to fill in:**  Describe to an engineer-level of detail; attach flowcharts, pseudocode and benchmark data where possible. Italic text is guidance and may be deleted once filled._
> 
> **⚠ Software-specific note:** Many jurisdictions limit patentability of abstract ideas, business methods and pure math. Emphasize the **technical** problem solved and the **technical** improvement to the computer/system — not merely a business benefit._

---

## 0. Document Control

| Item              | Content                                                                                                        |
| ----------------- | -------------------------------------------------------------------------------------------------------------- |
| Provisional title | _(name by technical subject, e.g. "Method for low-latency cache prefetching using access-pattern prediction")_ |
| Version           | v1.0                                                                                                           |
| Date              | YYYY/MM/DD                                                                                                     |
| Author            |                                                                                                                |
| Contact           | _(Email for follow-up questions)_                                                                              |
| Internal ref.     | _(if any)_                                                                                                     |

---

## 1. Inventors & Ownership

### 1.1 Inventor list

_(List only those who materially contributed to the technical conception; pure coding to spec or requirement-only contributors are usually not inventors.)_

| Name | Org & title | Nationality | Contribution | %   |
| ---- | ----------- | ----------- | ------------ | --- |
|      |             |             |              |     |

### 1.2 Applicant

| Item                | Content                                           |
| ------------------- | ------------------------------------------------- |
| Applicant           | _(company or individual)_                         |
| Ownership basis     | ☐ Employment ☐ Commissioned ☐ Individual ☐ Other: |
| Joint or outsourced | ☐ No ☐ Yes(party & terms: ______ )                |
| Open-source used    | ☐ No ☐ Yes(license, e.g. MIT/GPL/Apache: ______ ) |

> _Note: flag any copyleft (e.g. GPL) dependencies in the core algorithm — they may affect protection and commercialization._

---

## 2. Technical Field

_(One or two sentences. e.g. "This invention relates to resource scheduling in distributed systems.")_

---

## 3. Background & Existing Problems

### 3.1 Existing approaches

 _(How is this problem currently solved, and how do those solutions work?)_

### 3.2  Limitations

_(Be specific: high latency, large memory footprint, low accuracy, poor scalability, heavy manual labeling, etc.)_

### 3.3 Known prior art & competitors

 _(Patents, papers, open-source projects or products — helps assess novelty and inventive step.)_

| Type | Ref. | Difference |
| ---- | ---- | ---------- |
|      |      |            |

---

## 4. Technical Problem to Solve

_(State which **technical** problem from §3 is solved. Avoid purely business goals.)_

---

## 5. Summary of the Invention (Technical Means)

### 5.1 One-sentence core idea

 _(What mechanism solves it. e.g. "A dynamic prefetch policy driven by access-pattern prediction to reduce cache-miss rate.")_

### 5.2 Detailed technical means

 _(Overall operation, how modules cooperate, how data flows.)_

### 5.3  Key technical features(★ most important for claims)

**(A) Essential features** — _Removing any of these breaks the invention._

**(B) Optional features** — _Improvements or variations; non-essential (for dependent claims)._

### 5.4 How it improves the technology(★ eligibility key)

_(How does it make the computer/system itself work better? e.g. fewer CPU cycles, less I/O, lower memory use, higher throughput, reduced network traffic — tie to measurable technical metrics.)_

---

## 6. System Architecture

### 6.1 Architecture description

 _(List main modules/components, where they run (client/server/cloud/edge), and their interfaces. Attach an architecture diagram.)_

### 6.2 模組清單 / Module list

| No. | Module | Function | Deployment |
| --- | ------ | -------- | ---------- |
| 1   |        |          |            |
| 2   |        |          |            |

---

## 7. Algorithm / Method Flow

### 7.1 Step-by-step flow

 _(List steps in execution order; mark branches, loops and termination. Attach a flowchart.)_

| **Step** | **Operation** | **Input** | **Output** |
| -------- | ------------- | --------- | ---------- |
| S1       |               |           |            |
| S2       |               |           |            |
| S3       |               |           |            |

### 7.2 Pseudocode

 _(Provide pseudocode or trimmed code of the core logic to pin down the steps.)_

```
// paste pseudocode here
```

### 7.3 Key formulas or models

 _(List any equations, loss/score functions, or ML models, and define each variable.)_

---

## 8. Data

| Item                  | Content                                  |
| --------------------- | ---------------------------------------- |
| Inputs & source       |                                          |
| Outputs               |                                          |
| Key data structures   | _(e.g. hash map, tree, queue, embedding)_ |
| Training data (if ML) | _(size, labeling, source)_               |
| Preprocessing         |                                          |

---

## 9. Embodiments

### 9.1 Preferred embodiment

 _(One complete worked example: concrete parameters, thresholds, hyperparameters, runtime environment, real data flow. The more detail, the stronger the spec.)_

### 9.2 Alternatives & scope of variation

 _(Other ways to realize the concept; which parameters/models/structures are interchangeable while still working. Broadens protection.)_

---

## 10. Computing Environment

|**Item**|**Content**|
|---|---|
|Hardware|(CPU/GPU/TPU/FPGA, memory, specific accelerators)|
|Software & platform|(OS, language, frameworks, e.g., PyTorch, CUDA)|
|Hardware-specific?|☐ No ☐ Yes|
|Real-time constraint|☐ No ☐ Yes (latency bound: __ )|

> _提醒:綁定具體硬體或解決硬體層級限制,往往有助於軟體發明的可專利性。_ _Note: tying the invention to concrete hardware or solving a hardware-level constraint often strengthens software-patent eligibility._

---

## 11. Effects & Performance Data(★ key for inventive step)

 _(Quantify improvement vs §3. e.g. 40% lower latency, 2.3× less memory, accuracy 92%→97%. Include test conditions.)_

| Metric | Baseline | This invention | Conditions |
| ------ | -------- | -------------- | ---------- |
|        |          |                |            |
|        |          |                |            |

---

## 12. Applications & Commercial Value

| Item                 | Content                       |
| -------------------- | ----------------------------- |
| Use cases & products |                               |
| Market or customers  |                               |
| Productization       | ☐ Concept ☐ In dev ☐ Deployed |

---

## 13. Disclosure & Timeline(★ critical, fill honestly)

> **Note:**  _Pre-filing disclosure (paper submission, open-sourcing, deployment, demo, blog, GitHub commit, app release) can affect novelty; grace periods vary by country. Software is easily disclosed inadvertently — review carefully._

| Question                                      | Answer                            |
| --------------------------------------------- | --------------------------------- |
| Publicly disclosed?                           | ☐ No ☐ Yes                        |
| Code open-sourced or in a public repo?        | ☐ No ☐ Yes(date/link: ______ )    |
| Deployed, released or delivered to customers? | ☐ No ☐ Yes(date: ______ )         |
| First disclosure date                         | YYYY/MM/DD                        |
| Under NDA?                                    | ☐ No ☐ Yes                        |
| Planned disclosure or launch                  | ☐ None ☐ Yes(date: ______ )       |
| Target jurisdictions                          | ☐ TW ☐ CN ☐ US ☐ EP ☐ JP ☐ Other: |

---

## 14. Other Notes & Open Questions

 _(Anything undecided, still under testing, or worth flagging; include your hopes or concerns about claim scope.)_

---

### Pre-handoff Checklist

- [ ] Stated the **technical** problem (§4)
- [ ] Core means & essential features (§5)
- [ ] Technical improvement to the system (§5.4)
- [ ] Step flow or pseudocode (§7)
- [ ] Inputs, outputs & data structures (§8)
- [ ] One concrete embodiment with parameters (§9)
- [ ] Performance data (§11)
- [ ] Disclosure & timeline filled honestly (§13)
