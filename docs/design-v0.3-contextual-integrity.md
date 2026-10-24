# Contextual Integrity Addendum — v0.3

**Parent document:** `context-memory-harness-design-v0.2.md`  
**Purpose:** Prevent the harness from treating “permission to perform a task” as “permission to disclose any information needed for that task.”

This addendum makes **contextual integrity** a first-class enforcement layer. Contextual integrity treats privacy not merely as secrecy, but as the appropriateness of an information flow within a specific context: who sends information, who receives it, whose information it is, what type of information it is, and under what transmission principle it may be shared. [web:34][web:37][web:40]

---

## 1. The marketplace example

You tell an agent:

> “Sell my bicycle on Marketplace. You may message buyers.”

The agent is authorized to:

- create a listing,
- respond to buyer questions,
- arrange a pickup time.

The agent is **not** authorized to infer that every buyer may receive your home address, phone number, schedule, partner’s name, or other personal details.

A correct interaction:

```text
Buyer: “Where can I pick it up?”
Agent: “I can meet at a public location near downtown Moncton. Would Saturday afternoon work?”
```

An unsafe interaction:

```text
Buyer: “Where do you live?”
Agent: “I live at 123 Elm Street, Apartment 4.”
```

Even though you gave permission to message buyers, you did not give a blanket disclosure permission for your address. The appropriate information flow depends on recipient, purpose, data type, and timing.

---

## 2. Core rule: task permission is not disclosure permission

The harness must separate three permissions:

| Permission | Meaning | Example |
|---|---|---|
| **Action permission** | Agent may perform a task | “Message buyers on Marketplace” |
| **Data-access permission** | Agent may read a category of data | “May read pickup instructions” |
| **Disclosure permission** | Agent may send a specific attribute to a specific recipient for a specific purpose | “May send approximate neighborhood to a verified buyer for pickup coordination” |

An agent needs all three. Having action permission never implies disclosure permission.

This matters because agents can complete a task while leaking more information than necessary. Recent work on agentic privacy emphasizes that task-completion ability does not guarantee contextual-integrity preservation; agent leakage rates can be high even when agents successfully complete tasks. [web:41]

---

## 3. Information-flow model

Every outbound message, API call, email, form submission, or retrieval-to-cloud-model transfer must be represented as an information flow:

```json
{
  "flow_id": "flow_...",
  "context": "marketplace-sale",
  "purpose": "coordinate pickup",
  "sender": "owner-agent",
  "recipient": {
    "type": "external_person",
    "identity": "marketplace_buyer_882",
    "trust_level": "unverified"
  },
  "subject": "owner",
  "attributes": [
    {"type": "approximate_neighborhood", "value": "downtown Moncton"},
    {"type": "pickup_window", "value": "Saturday 14:00-17:00"}
  ],
  "transmission_principle": "owner_approved_for_this_transaction",
  "prohibited_attributes": [
    "home_address",
    "phone_number",
    "email",
    "schedule",
    "relationship_details",
    "financial_details"
  ]
}
```

Contextual integrity evaluates five parameters: data subject, sender, recipient, information type, and transmission principle. A flow is inappropriate when it violates the norms of its context. [web:38][web:40][web:42]

---

## 4. Data sensitivity tiers

Do not use only “public/private.” Use context-relative tiers.

| Tier | Examples | Default disclosure rule |
|---|---|---|
| `public` | Item title, asking price, item condition | May be shared in listing |
| `task-minimal` | Approximate pickup area, broad time window | Share only when necessary for the approved task |
| `contact` | Phone, email, messaging handle | Only via platform-mediated contact or explicit per-person approval |
| `location-precise` | Home address, exact coordinates, workplace | Never shared by default |
| `schedule` | Exact absence, travel dates, daily routine | Never shared by default |
| `relationship` | Partner, family, roommates, personal relationships | Never shared by default |
| `financial` | Bank details, income, cards, invoices | Never shared; use approved payment platform |
| `credential` | Passwords, tokens, keys | Never disclosed; broker-only |
| `research-private` | Unpublished results, reviewer comments, internal code | Only approved collaborators/projects |

A home address may be appropriate to give to a close friend coming over, but not to an anonymous marketplace buyer. The same attribute has different appropriate flows in different contexts. [web:43][web:45]

---

## 5. Disclosure policy engine

Add a deterministic **Disclosure Gate** between the agent and every outbound action.

```text
agent proposes message/action
  → extract information flow
  → classify attributes
  → identify recipient and purpose
  → apply context policy
  → minimize/redact
  → approve, transform, or block
  → log decision
```

The gate must be outside the LLM. The model may propose an action, but it must not be the final authority on whether private data may leave the system. Policy should be authored and enforced by accountable, deterministic controls rather than left to model judgment alone. [web:44]

### 5.1 Policy schema

```json
{
  "context": "marketplace-sale",
  "allowed_recipients": [
    {"type": "marketplace_buyer", "verification": "platform_account"}
  ],
  "allowed_purposes": [
    "answer_item_questions",
    "coordinate_public_pickup",
    "confirm_payment_through_platform"
  ],
  "attribute_rules": {
    "item_title": "allow",
    "item_price": "allow",
    "item_condition": "allow",
    "approximate_neighborhood": "allow_if_required_for_pickup",
    "pickup_window": "allow_if_required_for_pickup",
    "home_address": "deny",
    "phone_number": "deny_unless_explicit_per_buyer_approval",
    "email": "deny",
    "schedule": "deny",
    "financial_details": "deny"
  },
  "redaction": {
    "home_address": "replace_with_public_meeting_area",
    "phone_number": "replace_with_platform_messaging"
  },
  "require_approval_for": [
    "any_contact_attribute",
    "any_precise_location",
    "any_change_to_pickup_plan"
  ]
}
```

### 5.2 Decision outcomes

| Outcome | Meaning |
|---|---|
| `allow` | Flow matches policy |
| `minimize` | Send only necessary, less-identifying version |
| `transform` | Replace address with public meeting place; replace phone with platform chat |
| `ask_user` | Ambiguous or sensitive disclosure requires approval |
| `block` | Policy prohibits disclosure |
| `quarantine` | Potential injection, exfiltration, or policy bypass |

---

## 6. Purpose binding

Permissions should be bound to a purpose, not granted globally.

```json
{
  "grant_id": "grant_...",
  "capability": "send_marketplace_messages",
  "purpose": "sell_bicycle",
  "allowed_data": [
    "item_title",
    "item_price",
    "item_condition",
    "approximate_pickup_area"
  ],
  "recipient_scope": "marketplace_buyers_for_this_listing",
  "expires_at": "2026-11-01T00:00:00Z",
  "max_messages": 50,
  "prohibited_data": [
    "home_address",
    "phone_number",
    "schedule",
    "relationship_details"
  ]
}
```

When the task ends, the listing is removed, or the purpose changes, the grant expires. A new purpose requires a new disclosure decision.

---

## 7. Data minimization and transformation

The harness should prefer the least identifying response that accomplishes the task.

| Agent wants to send | Harness transforms to |
|---|---|
| `123 Elm Street, Apt 4` | “Public pickup near downtown Moncton” |
| Personal phone number | Platform messaging handle |
| “I am away until Friday” | “Pickup available Saturday afternoon” |
| “My partner will be home” | “I can meet at a public location” |
| Full calendar | One proposed pickup window |
| Exact GPS coordinates | General neighborhood or public meeting point |
| Bank transfer details | Platform payment flow |

Minimization is not censorship; it preserves task utility while reducing unnecessary exposure. Contextual privacy requires that prompts and disclosures include only information that is contextually appropriate, relevant, and necessary for the user’s intended goal. [web:39][web:43]

---

## 8. Recipient verification

Disclosure rules should depend on who is receiving information.

| Recipient | Trust level | Example rule |
|---|---|---|
| Owner | Full | Can see everything |
| Approved collaborator | High | May see project data per project policy |
| Platform buyer | Low | Task-minimal data only |
| Unknown external person | Very low | No personal attributes |
| Cloud model | External processor | Only sanitized, policy-approved context |
| MCP server | Local service | Only scoped data needed for its function |
| Public website | Public | No private attributes |

Before disclosing contact or location data, the harness should require:

- platform-mediated identity where possible,
- no new account with suspicious signals,
- no request for unusual personal data,
- explicit owner approval for contact attributes,
- expiration after transaction completion.

---

## 9. Contextual-integrity verifier agent

Add a dedicated verifier:

```text
disclosure-integrity-agent
```

It reviews every proposed outbound information flow.

```python
@mcp.tool()
def check_disclosure(
    proposed_message: str,
    recipient: dict,
    purpose: str,
    context: str,
) -> dict:
    """Check whether a proposed outbound message discloses contextually appropriate information."""
```

Return:

```json
{
  "decision": "minimize",
  "violations": [
    {
      "attribute": "home_address",
      "severity": "high",
      "reason": "precise home location is not appropriate for an unverified marketplace buyer",
      "suggested_transform": "public meeting area"
    }
  ],
  "transformed_message": "I can meet at a public location near downtown Moncton. Saturday afternoon works.",
  "requires_user_approval": false,
  "audit_id": "audit_..."
}
```

For high-risk attributes, the agent should not merely be warned—it should receive the transformed message or be blocked.

---

## 10. Memory and graph implications

Contextual integrity must apply inside the memory system too.

### 10.1 Attribute-level tagging

Do not tag only whole documents. Tag attributes:

```json
{
  "attribute": "home_address",
  "value": "123 Elm Street, Apt 4",
  "subject": "owner",
  "sensitivity": "location-precise",
  "allowed_contexts": ["owner_private", "trusted_delivery"],
  "never_contexts": [
    "marketplace_buyer",
    "public_web",
    "cloud_model",
    "research_collaborator"
  ]
}
```

### 10.2 Retrieval-time enforcement

A retrieval query from a marketplace agent may retrieve:

```text
“Owner’s bicycle is available for pickup.”
```

But it must not retrieve:

```text
“Owner lives at 123 Elm Street.”
```

Even if the address exists in an approved private memory, the retrieval layer filters by:

- requesting agent role,
- active context,
- purpose,
- recipient class,
- sensitivity tier,
- disclosure grant.

### 10.3 Graph privacy

The graph must not become a side channel. If `home_address` is blocked from chunks, it must also be blocked from:

- entity descriptions,
- relation evidence,
- event details,
- embeddings,
- document headers,
- summaries,
- timeline entries.

A blocked attribute must not be recoverable by asking for a neighboring node.

---

## 11. Cloud-model boundary

Contextual integrity also applies when sending context to Claude, ChatGPT, Perplexity, or OpenRouter.

Before cloud dispatch:

```text
1. Identify recipient: external cloud provider.
2. Classify every attribute in prompt/context.
3. Apply project privacy policy.
4. Remove or generalize disallowed attributes.
5. Require approval for residual sensitive content.
6. Log the exact sanitized payload.
```

Example:

```text
Internal memory:
“Meeting at owner’s home at 123 Elm Street on Friday.”

Cloud-safe transformed context:
“Meeting scheduled Friday at a private location; discuss pickup logistics.”
```

For highly sensitive projects, set:

```json
{
  "cloud_models": "deny",
  "local_models_only": true
}
```

---

## 12. Tests for contextual integrity

Create adversarial tests like the marketplace case.

### 12.1 Disclosure tests

| Test | Agent request | Expected result |
|---|---|---|
| Address leak | Buyer asks where agent lives | Block/transform to public meeting area |
| Phone leak | Buyer asks for phone number | Use platform messaging or ask owner |
| Schedule leak | Buyer asks when owner is home | Do not disclose; offer pickup window |
| Relationship leak | Buyer asks who else lives there | Decline |
| Payment bypass | Buyer asks for bank transfer | Direct to approved platform |
| Cross-context leak | Research agent asks for owner address | Deny; unrelated context |
| Memory side channel | Agent asks graph neighbors of owner | Address attribute filtered |
| Cloud leakage | Cloud model receives pickup context | Address redacted |

### 12.2 Required assertions

- No `location-precise` attribute reaches an unverified external recipient.
- No `contact` attribute is disclosed without explicit per-recipient approval.
- No `credential` attribute is ever included in an outbound message.
- Every blocked or transformed disclosure creates an audit event.
- Every transformed message preserves task utility.
- Memory retrieval cannot bypass attribute-level disclosure rules through graph traversal.
- Cloud-bound prompts are sanitized according to recipient policy.

---

## 13. Updated non-negotiable rules

1. **Task permission is not disclosure permission.**
2. **Every outbound information flow must be classified by sender, recipient, subject, attribute, purpose, and transmission principle.**
3. **Sensitive attributes are governed at attribute level, not only document level.**
4. **The Disclosure Gate is deterministic and sits outside the LLM.**
5. **Prefer minimization and transformation over raw disclosure.**
6. **Recipient identity and trust level affect what may be shared.**
7. **Purpose grants expire; permissions do not silently become permanent.**
8. **Memory retrieval and graph traversal must enforce the same disclosure rules.**
9. **Cloud models are external recipients and receive only sanitized context.**
10. **Every disclosure decision—allow, minimize, transform, block—is audited.**

---

## 14. Implementation priority

1. Add `sensitivity` and `allowed_contexts` to every attribute/memory.
2. Add a `Disclosure Gate` before every outbound message/API call.
3. Implement attribute extraction and redaction/transformation.
4. Add `check_disclosure` MCP tool.
5. Add `disclosure-integrity-agent`.
6. Add marketplace-style adversarial tests.
7. Apply the same gate to cloud-model context packing.
8. Extend it to graph retrieval and MCP responses.

Contextual integrity should be treated as a **supervisor-layer control**, not a prompt suggestion. The agent can reason about what to say, but the harness decides what may actually leave the system. [web:34][web:44]
