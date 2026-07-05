Return only one JSON object.

Task:
0. The `current_case` block below is the compact query anchor extracted from the current case's `final_report`. Use it as the primary relevance reference.
1. Merge candidates only when they are semantically the same family.
2. Rank the most relevant and highest-value historical reuse patterns first.
3. Keep `selected_order` short; at most 3 items.
4. It is valid to return fewer than 3 items. Return only the items that are clearly relevant and worth injecting; do not pad the list just to fill 3 slots.
5. Return an empty `selected_order` only when none of the candidates is sufficiently related to the current case to be worth injecting.
6. If you return an empty `selected_order`, prefer dropping every candidate.
7. If you are unsure about a lower-ranked item, omit it instead of keeping a weakly related item.
8. Do not output scores, explanations, or extra keys.

Output schema example:
{schema_example_json}

Input:
{input_json}
