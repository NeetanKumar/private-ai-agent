# Future Scope: Private AI Agent Template

Enhancements beyond the five phases that are built. Nothing here is started. For defects and
limitations in what already exists, see `KNOWN_GAPS.md`.

- **Tamper-evident audit log.** Chain each audit record to the hash of the previous one, so deleting or editing an entry breaks the chain and hidden egress becomes provable.
- **Kernel-level egress proof.** Reconcile every outbound connection the kernel observes against an audit record continuously via eBPF, halting the gateway on a mismatch.
- **Confidential computing.** Run the stack inside a trusted execution environment so a rented host's operator cannot read memory at all.
