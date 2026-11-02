# Roles, groups and the activity log

Status: tasks 1 and 2 of [the identity plan](../superpowers/plans/2026-11-01-collaboration-identity.md) are built. Sign-in (OpenID Connect), sessions, edit leases, presence and comments are still to do; see the plan and [the design](../superpowers/specs/2026-11-01-collaboration-identity-design.md).

## Roles

Three, in order: `viewer` (reads everything the desk shows, writes nothing), `operator` (runs the show) and `admin` (operator, plus users, groups and destructive actions). Every POST route is classified in `serve.ROUTE_ROLES`; a route that is not classified needs `admin`. The check is a rank comparison (`volt/access.py` `RANK`), so a viewer on an operator route is `403 forbidden_role`. This reverses the earlier "operator and admin only" ruling; the viewer exists for phones, outside colleagues and read-only monitors.

## Groups and the effective role

A user has a direct role (`volt user add NAME --role viewer|operator|admin`, `volt user role NAME --role ...`, `POST /api/users/{name}/role`). A group (`volt user group-add NAME [--role R]`, `group-member`, or `POST /api/groups`, `/api/groups/{name}/members` with `{add, remove}`, `/api/groups/{name}/delete`; `GET /api/groups`) may carry a role. A user's **effective role** is the highest of their direct role and their groups' roles, so a group can only raise access. It is read on every request: removing someone from a group, deleting a group, or disabling a user takes effect on the next request, for the same token. `GET /api/whoami` returns the effective `role` and the user's `groups`. Group and role routes are admin-only and audited (`group_create`, `group_members`, `group_delete`, `role`). Demoting the last enabled admin is `409 last_admin` unless a legacy `VOLT_TOKEN` still administers. Migration 22 adds `volt_user.email`, `display_name`, `source` (for the sign-in work) and the `volt_group` and `group_member` tables.

## The activity log

`GET /api/audit` keeps its original form for any operator: `?target=<id>` returns that target's rows. With no `target`, or with any filter, it is admin-only: `actor`, `action`, `kind`, `target`, `from`, `to` (inclusive time bounds), `before` (the cursor from the previous page), `limit` (1 to 500, default 100) and `format=csv`. Rows come newest first in `(at, id)` order, so rows with equal times neither repeat nor go missing across pages; the response carries `next`, the cursor for the following page, when there is one. The CSV puts a single quote in front of any cell that starts with `=`, `+`, `-`, `@`, tab or carriage return, so a spreadsheet shows it as text instead of running it. Migration 23 adds the indexes. The Activity screen (the admin's "Activity" link in the header pill, or `#activity`) shows the table with filters, "Load more" and a "Download CSV" that is fetched with the token.

## What the tests prove

Rank and effective-role rules, immediate effect of membership changes, the last-admin rule, routes classified and admin-only, audited, paging without gaps or repeats on equal times, filters, CSV neutralisation round-tripped through a CSV reader, the original `target=` call unchanged. Not yet proved on Postgres in this session (CI runs the Postgres jobs); nothing here involves a real identity provider.
