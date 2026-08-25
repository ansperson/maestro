# Historical payment decision

This obsolete ADR says that each Order has exactly one Payment. The current model and schema
contradict it, so a verifier must report the conflict rather than silently choosing the ADR.
