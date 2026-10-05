<!-- changelog: security -->
- **Review autofix keeps Claude command admission fixed throughout an editor run.** A command's template twin must exist when the sandbox snapshot is taken. Adding the twin during a transfer cannot make the command eligible on a later retry. Missing or malformed admission state stops the transfer rather than falling back to the mutable host checkout.
