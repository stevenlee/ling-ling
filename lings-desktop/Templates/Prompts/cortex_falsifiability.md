You are assessing the falsifiability (empirical content) of a claim.
A claim has empirical content if and only if you can describe a concrete observation that would prove it false.

First, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim.
Then, score the claim from 0.0 to 1.0 based on how falsifiable it is:
- 1.0: The falsifier is a concrete, observable, specific scenario.
- 0.5: A falsifier exists but requires further operationalization to be tested.
- 0.0: The claim is unfalsifiable (e.g., vague absolute, tautology, value statement, or the falsifier is just 'when it is not true').

Return ONLY a JSON object:
{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific observation that refutes it, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>"}
