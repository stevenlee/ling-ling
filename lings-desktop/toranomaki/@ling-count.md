# 🔢 AnythingCounter Example Commands

To use the AnythingCounter, create a file in `toLingLing/` with one of the following formats.

---

## Example 1: Single Concept × Single Article
```markdown
@ling-count [[Article Name]]
Count: appeals to authority
```

## Example 2: Multiple Concepts × Single Article
```markdown
@ling-count [[Research Paper]]
Count: appeals to authority
Count: strawman arguments
Count: unsubstantiated claims
```

## Example 3: Single Concept × Multiple Articles
```markdown
@ling-count [[Article A]] [[Article B]] [[Article C]]
Count: cognitive bias
```

## Example 4: Multiple Concepts × Multiple Articles (Matrix)
```markdown
@ling-count [[Essay One]] [[Essay Two]]
Count: metaphors
Count: anecdotal evidence
Count: appeals to emotion
Confidence: high
```

This generates a cross-tabulation matrix:

| Article    | metaphors | anecdotal evidence | appeals to emotion | Total |
|------------|-----------|--------------------|--------------------|-------|
| Essay One  | 5         | 3                  | 2                  | 10    |
| Essay Two  | 2         | 7                  | 1                  | 10    |
| **Total**  | **7**     | **10**             | **3**              | **20**|

## Example 5: Natural Language Question
```markdown
/count [[Long Article]]
How many times does the author use personal anecdotes to support an argument?
```

---

### Options
- `Confidence: high|medium|low` — filter minimum confidence (default: `medium`)
- Use `[[WikiLink]]` to reference articles in pages/ or Notes/
- If no file match, Ling-Ling falls back to RAG semantic search
