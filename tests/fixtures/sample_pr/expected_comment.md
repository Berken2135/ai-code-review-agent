<!-- pr-review-agent -->
## 🤖 AI Code Review

**Verdict:** ❌ Changes requested

Adds discount and order lookup helpers. get_order builds its SQL by string concatenation, which is exploitable and should be fixed first. apply_discount and its tests do not cover out-of-range percentages.

> ⚠️ 1 file(s) were skipped (lockfiles, generated, vendored, binary or deleted).

### 🔴 High (1)

**SQL injection in get_order**  
`src/orders/service.py:9-10` · security · 95%

`order_id` is concatenated into the SQL string, so a crafted id can run arbitrary SQL.

> **Suggestion:** Use a parameterised query: `conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,))`.

### 🟠 Medium (1)

**apply_discount accepts discounts above 100%**  
`src/orders/service.py:5-10` · bug · 80%

A `pct` over 100 makes the price negative; negative percentages raise it.

> **Suggestion:** Validate `0 <= pct <= 100` and raise `ValueError` otherwise.

### 🧪 Suggested tests (1)

<details>
<summary>Cover discounts outside 0-100% — `src/orders/service.py`</summary>

Only the 10% happy path is tested; the boundary and invalid values are not.

Suggested location: tests/test_service.py.

```python
import pytest
from orders.service import apply_discount


@pytest.mark.parametrize("pct", [-1, 101])
def test_invalid_discount_is_rejected(pct):
    with pytest.raises(ValueError):
        apply_discount(100, pct)
```

</details>

---
<sub>Reviewed commit `c0ffee0` · Automated review, verify before acting.</sub>