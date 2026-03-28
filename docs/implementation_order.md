# Implementation Order

Follow this order unless explicitly instructed otherwise.

## Step 1
Bootstrap repo, local infra, health endpoint

## Step 2
DB schema and repository layer

## Step 3
LangGraph workflow skeleton with fake worker

## Step 4
Checkpoint persistence and approval interrupts

## Step 5
Sandbox workspace and artifact capture

## Step 6
First real coding worker

## Step 7
Webhook + Telegram ingress

## Step 8
Memory v1

## Step 9
Second worker and routing

## Step 10
Tools, observability, hardening

## Rule

Do not implement step N+1 until step N has:
- passing tests
- usable logs
- stable local run path
