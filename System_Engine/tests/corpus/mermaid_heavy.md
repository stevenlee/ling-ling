---
title: System Diagrams — Order Processing Pipeline
tags: [diagrams, architecture, mermaid]
---

# System Diagrams — Order Processing Pipeline

## High-Level Flow

The order processing pipeline has three stages: validation, payment,
fulfilment. Each stage emits domain events that downstream services
react to asynchronously.

```mermaid
flowchart LR
    A[Customer Order] --> B{Validation}
    B -->|valid| C[Payment Authorize]
    B -->|invalid| X[Reject]
    C -->|approved| D[Reserve Inventory]
    C -->|declined| X
    D -->|in stock| E[Capture Payment]
    D -->|backorder| F[Wait for Restock]
    E --> G[Fulfilment]
    F --> D
    G --> H[Ship]
    H --> I[Done]
```

The choice to authorize-then-capture (rather than charge once) lets us
handle partial fulfilment cleanly. If we run out of stock between
authorization and capture, we void the authorization rather than refund.

## State Machine of an Order

```mermaid
stateDiagram-v2
    [*] --> Submitted
    Submitted --> Validating
    Validating --> Invalid: validation failed
    Validating --> AwaitingPayment: validation ok
    AwaitingPayment --> PaymentDeclined: declined
    AwaitingPayment --> Reserved: authorized
    Reserved --> BackOrdered: out of stock
    Reserved --> Charged: stock confirmed
    BackOrdered --> Reserved: stock restored
    BackOrdered --> Cancelled: timeout
    Charged --> Fulfilling
    Fulfilling --> Shipped
    Shipped --> Delivered
    Delivered --> [*]
    Invalid --> [*]
    PaymentDeclined --> [*]
    Cancelled --> [*]
```

Note that an order in `Reserved` state has authorized payment but not
captured funds. Authorisations expire after 7 days at most card networks.

## Sequence: Successful Order

```mermaid
sequenceDiagram
    participant C as Customer
    participant API
    participant V as Validator
    participant P as Payments
    participant I as Inventory
    participant F as Fulfilment

    C->>API: POST /orders
    API->>V: validate(order)
    V-->>API: ok
    API->>P: authorize(amount)
    P-->>API: auth_id
    API->>I: reserve(items)
    I-->>API: reservation_id
    API-->>C: 201 Created
    Note over API,F: async from here
    API->>P: capture(auth_id)
    P-->>API: captured
    API->>F: fulfil(order)
    F-->>API: shipped
    API->>C: webhook: order_shipped
```

## Component Diagram

The runtime topology is small but the failure modes are not:

```mermaid
graph TB
    subgraph Frontend
        WEB[Web App]
        MOB[Mobile App]
    end

    subgraph Gateway
        APIGW[API Gateway]
        AUTH[Auth Service]
    end

    subgraph Core
        ORDERS[Order Service]
        PAY[Payment Service]
        INV[Inventory Service]
        FULFIL[Fulfilment Service]
    end

    subgraph Data
        ORDERDB[(Orders DB)]
        PAYDB[(Payments DB)]
        INVDB[(Inventory DB)]
        EVENTBUS[Event Bus]
    end

    WEB --> APIGW
    MOB --> APIGW
    APIGW --> AUTH
    APIGW --> ORDERS
    ORDERS --> PAY
    ORDERS --> INV
    ORDERS --> EVENTBUS
    PAY --> PAYDB
    INV --> INVDB
    ORDERS --> ORDERDB
    EVENTBUS --> FULFIL
    FULFIL --> EVENTBUS
```

## Failure Mode Map

```mermaid
mindmap
  root((Order Pipeline Failures))
    Validation
      Schema mismatch
      Missing fields
      Currency mismatch
      Geofence violation
    Payment
      Network timeout
      Card declined
      Fraud check rejected
      Idempotency key collision
    Inventory
      Race on reservation
      Stale stock count
      Multi-warehouse split failed
    Fulfilment
      Carrier API down
      Label generation failed
      Weight mismatch
    Eventing
      Lost message
      Duplicate delivery
      Out-of-order delivery
      Schema drift
```

## Notes

These diagrams are kept here as the canonical source. Code references
this file in PR descriptions ("update PaymentService — see
[[mermaid_heavy#Component Diagram]]"). When the architecture changes,
update the diagram first, then the code.
