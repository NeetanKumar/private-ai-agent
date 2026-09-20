# Platform API Guide

## Authentication
Access tokens are sent as bearer tokens and expire after 60 minutes.

## Pagination
List endpoints use cursor pagination. The maximum page size is 200 items.

## Idempotency
Send an Idempotency-Key header on POST requests. Keys are remembered for 24 hours.

## Errors
A 429 response includes a Retry-After header telling the client how many seconds to wait.

## Versions
Version 1 of the API is deprecated and will be shut down on 2027-03-31. Official SDKs exist for Python and Go.
