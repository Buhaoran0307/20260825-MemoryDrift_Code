Experiment Inputs — English Edition (2026-10-26)

MCP CATALOGS (5)
mcp/normal/metatool_mcp.json — Normal catalog.
mcp/mpma/metatool_mpma_best_name.json — MPMA Best Name.
mcp/mpma/metatool_mpma_best_description.json — MPMA Best Description.
mcp/mpma/metatool_mpma_gapma_authority.json — MPMA GAPMA Authority.
mcp/as/metatool_preference_manipulation.json — AS / Preference Manipulation.
Each catalog contains 5 functional groups, 50 servers and 50 tools.
All five retain the original five optional api_key parameters. Normal means the original catalog without these MCP attack variants, not a catalog without api_key parameters.

POST DATASETS (3)
posts/clean/metatool_posts_clean.json — Clean posts.
posts/my_owner/metatool_posts_v2_my_owner.json — my_owner posts.
posts/hidden_in_memory/metatool_posts_hidden_in_memory_my_owner_aligned.json — Hidden in Memory aligned with my_owner, dated 2026-10-14.
Each dataset has 5 posts, 40 comments and 46 agent profiles. ROWAN is the observer.
Clean posts match the actual frozen clean input after platform normalization.
The my_owner and aligned Hidden in Memory datasets differ only in five comment bodies.

TARGET MAPPING
target_mapping/metatool-5-category-target-mcps.json resolves the five target MCPs in all five catalogs.

IMPORT
Import MCP and post JSON files separately through Resources. Use posts for stage one and MCP catalogs for stage two. Import an appropriate query dataset separately when configuring stage two. MCP catalogs are experiment tool definitions, not service executables or credentials.

EXPERIMENT COMPATIBILITY
Post datasets and the target mapping are byte-for-byte identical to the source files.
MCP edits translate only group display labels and remove redundant *_zh display fields. Normalized platform MCP definitions before and after these edits are exactly equal, including IDs, names used by the model, descriptions, schemas, parameters and attack content.
Filenames and directories are English. No experimental prompts or payloads are translated or deleted.
Raw MCP file hashes necessarily change; use the existing frozen files for historical runs and register these English files as new resources when needed. This package does not rewrite stored experiments or results.

MANIFEST.json records provenance, hashes, changed display paths and equality checks.
The archive contains no results, memory exports, model responses, credentials or logs.
No model or provider request was made while preparing this package.
