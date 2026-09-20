# Orchard Sync Service Specification

## Plans
The free tier includes 5 GB of storage. The Pro plan costs $12 per month and includes 2 TB. The maximum file size on Pro is 50 GB.

## Sync behaviour
When two devices edit the same file, Orchard keeps both versions and names the second one "conflicted copy".

## Limits
The API rate limit is 600 requests per minute per token. Failed webhooks are retried 5 times with exponential backoff up to 1 hour.

## Infrastructure
Data is stored in the eu-west and us-east regions and encrypted with AES-256 at rest.
