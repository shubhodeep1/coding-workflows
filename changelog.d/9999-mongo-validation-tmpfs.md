<!-- changelog: fixed -->
- **Validation harnesses that start MongoDB run again in the sandbox.** The `mongo` service in the `python-mongo-flask` and `python-mongo-repo-checks` compose templates now mounts `/data/db` and `/data/configdb` as tmpfs.

Every consumer on those two templates failed validation at container creation with `failed to copy xattrs: failed to set xattr "system.posix_acl_default" ... invalid argument`. The image declares both paths as `VOLUME`. Docker therefore created anonymous volumes and copied the image content into them, and the validation sandbox's rootless daemon cannot set that ACL xattr. With tmpfs mounts at the same paths no volume is created, and the test database, which is thrown away after each run anyway, lives in memory.

| The numbers that matter | Value |
| --- | --- |
| Consumers red for this reason in the 2026-10-18 validation refresh | 5 (atlas-bridge.gd, binance-blessings, drhyg_ecommerce_automation, radateeree-resort.com, tele-funtoken-msg-scoring) |
| Templates changed | 2 |

What this means for operators: the next validation refresh re-renders `validation/docker-compose.test.yml` for those consumers with the tmpfs mounts. No repository variable or consumer change is needed.
