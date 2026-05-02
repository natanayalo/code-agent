# Arize-Phoenix REST API Manual
Version: 1.0

This manual is auto-generated from the Phoenix OpenAPI specification.

## Annotation_configs

### List annotation configurations
`GET /v1/annotation_configs`

Retrieve a paginated list of all annotation configurations in the system.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| cursor | query | string | Cursor for pagination (base64-encoded annotation config ID) |
| limit | query | integer | Maximum number of configs to return |

#### Responses
| Code | Description |
| --- | --- |
| 200 | A list of annotation configurations with pagination information |
| 403 | Forbidden |
| 422 | Validation Error |

### Create an annotation configuration
`POST /v1/annotation_configs`

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 422 | Validation Error |

### Get an annotation configuration by ID or name
`GET /v1/annotation_configs/{config_identifier}`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| config_identifier | path | string | ID or name of the annotation configuration |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 422 | Validation Error |

### Update an annotation configuration
`PUT /v1/annotation_configs/{config_id}`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| config_id | path | string | ID of the annotation configuration |

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 422 | Validation Error |

### Delete an annotation configuration
`DELETE /v1/annotation_configs/{config_id}`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| config_id | path | string | ID of the annotation configuration |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 422 | Validation Error |

## Annotations

### Get span annotations filtered by span_ids and/or identifier.
`GET /v1/projects/{project_identifier}/span_annotations`

Return span annotations for a project, filtered by `span_ids`, `identifier`, or both. At least one of `span_ids` or `identifier` must be supplied. When both are supplied, results are the AND-intersection of the two filters.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| project_identifier | path | string | The project identifier: either project ID or project name. If using a project name as the identifier, it cannot contain slash (/), question mark (?), or pound sign (#) characters. |
| span_ids | query | array | Optional list of span ids to fetch annotations for. If omitted, `identifier` must be supplied. |
| identifier | query | array | Optional list of annotation identifiers to filter by. Each value must be non-empty. If omitted, `span_ids` must be supplied. When combined with `span_ids`, results are the AND-intersection of both filters. |
| include_annotation_names | query | array | Optional list of annotation names to include. If provided, only annotations with these names will be returned. 'note' annotations are excluded by default unless explicitly included in this list. |
| exclude_annotation_names | query | array | Optional list of annotation names to exclude from results. |
| cursor | query | string | A cursor for pagination |
| limit | query | integer | The maximum number of annotations to return in a single request |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 404 | Project or spans not found |
| 422 | Invalid parameters |

### Get trace annotations filtered by trace_ids and/or identifier.
`GET /v1/projects/{project_identifier}/trace_annotations`

Return trace annotations for a project, filtered by `trace_ids`, `identifier`, or both. At least one of `trace_ids` or `identifier` must be supplied. When both are supplied, results are the AND-intersection of the two filters.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| project_identifier | path | string | The project identifier: either project ID or project name. If using a project name as the identifier, it cannot contain slash (/), question mark (?), or pound sign (#) characters. |
| trace_ids | query | array | Optional list of trace ids to fetch annotations for. If omitted, `identifier` must be supplied. |
| identifier | query | array | Optional list of annotation identifiers to filter by. Each value must be non-empty. If omitted, `trace_ids` must be supplied. When combined with `trace_ids`, results are the AND-intersection of both filters. |
| include_annotation_names | query | array | Optional list of annotation names to include. If provided, only annotations with these names will be returned. 'note' annotations are excluded by default unless explicitly included in this list. |
| exclude_annotation_names | query | array | Optional list of annotation names to exclude from results. |
| cursor | query | string | A cursor for pagination |
| limit | query | integer | The maximum number of annotations to return in a single request |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 404 | Project or traces not found |
| 422 | Invalid parameters |

### Get session annotations filtered by session_ids and/or identifier.
`GET /v1/projects/{project_identifier}/session_annotations`

Return session annotations for a project, filtered by `session_ids`, `identifier`, or both. At least one of `session_ids` or `identifier` must be supplied. When both are supplied, results are the AND-intersection of the two filters.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| project_identifier | path | string | The project identifier: either project ID or project name. If using a project name as the identifier, it cannot contain slash (/), question mark (?), or pound sign (#) characters. |
| session_ids | query | array | Optional list of session ids to fetch annotations for. If omitted, `identifier` must be supplied. |
| identifier | query | array | Optional list of annotation identifiers to filter by. Each value must be non-empty. If omitted, `session_ids` must be supplied. When combined with `session_ids`, results are the AND-intersection of both filters. |
| include_annotation_names | query | array | Optional list of annotation names to include. If provided, only annotations with these names will be returned. 'note' annotations are excluded by default unless explicitly included in this list. |
| exclude_annotation_names | query | array | Optional list of annotation names to exclude from results. |
| cursor | query | string | A cursor for pagination |
| limit | query | integer | The maximum number of annotations to return in a single request |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 404 | Project or sessions not found |
| 422 | Invalid parameters |

## Datasets

### List datasets
`GET /v1/datasets`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| cursor | query | string | Cursor for pagination |
| name | query | string | An optional dataset name to filter by |
| limit | query | integer | The max number of datasets to return at a time. |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 422 | Unprocessable Content |

### Delete dataset by ID
`DELETE /v1/datasets/{id}`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| id | path | string | The ID of the dataset to delete. |

#### Responses
| Code | Description |
| --- | --- |
| 204 | Successful Response |
| 403 | Forbidden |
| 404 | Dataset not found |
| 422 | Invalid dataset ID |

### Get dataset by ID
`GET /v1/datasets/{id}`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| id | path | string | The ID of the dataset |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Validation Error |

### List dataset versions
`GET /v1/datasets/{id}/versions`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| id | path | string | The ID of the dataset |
| cursor | query | string | Cursor for pagination |
| limit | query | integer | The max number of dataset versions to return at a time |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 422 | Unprocessable Content |

### Upload dataset from JSON, JSONL, CSV, or PyArrow
`POST /v1/datasets/upload`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| sync | query | boolean | If true, fulfill request synchronously and return JSON containing dataset_id. |

#### Request Body
- Content-Type: `application/json`
- Content-Type: `multipart/form-data`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 409 | Dataset with the given name already exists (action=create). |
| 422 | Invalid request body |

### Get examples from a dataset
`GET /v1/datasets/{id}/examples`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| id | path | string | The ID of the dataset |
| version_id | query | string | The ID of the dataset version (if omitted, returns data from the latest version) |
| split | query | array | List of dataset split identifiers (GlobalIDs or names) to filter by |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Validation Error |

### Download dataset examples as CSV file
`GET /v1/datasets/{id}/csv`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| id | path | string | The ID of the dataset |
| version_id | query | string | The ID of the dataset version (if omitted, returns data from the latest version) |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 422 | Unprocessable Content |

### Download dataset examples as JSONL file
`GET /v1/datasets/{id}/jsonl`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| id | path | string | The ID of the dataset |
| version_id | query | string | The ID of the dataset version (if omitted, returns data from the latest version) |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 422 | Invalid dataset or version ID |

### Download dataset examples as OpenAI fine-tuning JSONL file
`GET /v1/datasets/{id}/jsonl/openai_ft`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| id | path | string | The ID of the dataset |
| version_id | query | string | The ID of the dataset version (if omitted, returns data from the latest version) |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 422 | Invalid dataset or version ID |

### Download dataset examples as OpenAI evals JSONL file
`GET /v1/datasets/{id}/jsonl/openai_evals`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| id | path | string | The ID of the dataset |
| version_id | query | string | The ID of the dataset version (if omitted, returns data from the latest version) |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 422 | Invalid dataset or version ID |

## Experiments

### Create experiment on a dataset
`POST /v1/datasets/{dataset_id}/experiments`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| dataset_id | path | string | - |

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Experiment retrieved successfully |
| 403 | Forbidden |
| 404 | Dataset or DatasetVersion not found |
| 422 | Validation Error |

### List experiments by dataset
`GET /v1/datasets/{dataset_id}/experiments`

Retrieve a paginated list of experiments for the specified dataset.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| dataset_id | path | string | - |
| cursor | query | string | Cursor for pagination (base64-encoded experiment ID) |
| limit | query | integer | The max number of experiments to return at a time. |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Paginated list of experiments for the dataset |
| 403 | Forbidden |
| 422 | Unprocessable Content |

### Get experiment by ID
`GET /v1/experiments/{experiment_id}`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| experiment_id | path | string | - |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Experiment retrieved successfully |
| 403 | Forbidden |
| 404 | Experiment not found |
| 422 | Validation Error |

### Delete experiment by ID
`DELETE /v1/experiments/{experiment_id}`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| experiment_id | path | string | - |
| delete_project | query | boolean | If true, also delete the project associated with the experiment that contains traces and spans for the experiment tasks. |

#### Responses
| Code | Description |
| --- | --- |
| 204 | Experiment deleted successfully |
| 403 | Forbidden |
| 404 | Experiment not found |
| 422 | Validation Error |

### Get incomplete runs for an experiment
`GET /v1/experiments/{experiment_id}/incomplete-runs`

Get runs that need to be completed for this experiment.

Returns all incomplete runs, including both missing runs (not yet attempted)
and failed runs (attempted but have errors).

Args:
    experiment_id: The ID of the experiment
    cursor: Cursor for pagination
    limit: Maximum number of results to return

Returns:
    Paginated list of incomplete runs grouped by dataset example,
    with repetition numbers that need to be run

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| experiment_id | path | string | - |
| cursor | query | string | Cursor for pagination |
| limit | query | integer | Maximum number of examples with incomplete runs to return |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Incomplete runs retrieved successfully |
| 403 | Forbidden |
| 404 | Experiment not found |
| 422 | Invalid cursor format |

### Download experiment runs as a JSON file
`GET /v1/experiments/{experiment_id}/json`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| experiment_id | path | string | - |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 404 | Experiment not found |
| 422 | Validation Error |

### Download experiment runs as a CSV file
`GET /v1/experiments/{experiment_id}/csv`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| experiment_id | path | string | - |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 422 | Validation Error |

### Create run for an experiment
`POST /v1/experiments/{experiment_id}/runs`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| experiment_id | path | string | - |

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Experiment run created successfully |
| 403 | Forbidden |
| 404 | Experiment or dataset example not found |
| 409 | Experiment run already exists with a successful result and cannot be updated |
| 422 | Validation Error |

### List runs for an experiment
`GET /v1/experiments/{experiment_id}/runs`

Retrieve a paginated list of runs for an experiment

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| experiment_id | path | string | - |
| cursor | query | string | Cursor for pagination (base64-encoded experiment run ID) |
| limit | query | integer | The max number of experiment runs to return at a time. If not specified, returns all results. |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Experiment runs retrieved successfully |
| 403 | Forbidden |
| 404 | Experiment not found |
| 422 | Invalid cursor format |

### Get incomplete evaluations for an experiment
`GET /v1/experiments/{experiment_id}/incomplete-evaluations`

Get experiment runs that have incomplete evaluations.

Returns runs with:
- Missing evaluations (evaluator has not been run)
- Failed evaluations (evaluator ran but has errors)

Args:
    experiment_id: The ID of the experiment
    evaluation_name: List of evaluation names to check (required, at least one)
    cursor: Cursor for pagination
    limit: Maximum number of results to return

Returns:
    Paginated list of runs with incomplete evaluations

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| experiment_id | path | string | - |
| evaluation_name | query | array | Evaluation names to check |
| cursor | query | string | Cursor for pagination |
| limit | query | integer | Maximum number of runs with incomplete evaluations to return |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Incomplete evaluations retrieved successfully |
| 403 | Forbidden |
| 400 | No evaluator names provided |
| 404 | Experiment not found |
| 422 | Invalid cursor format |

### Create or update evaluation for an experiment run
`POST /v1/experiment_evaluations`

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 404 | Experiment run not found |
| 422 | Validation Error |

## Projects

### List all projects
`GET /v1/projects`

Retrieve a paginated list of all projects in the system.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| cursor | query | string | Cursor for pagination (project ID) |
| limit | query | integer | The max number of projects to return at a time. |
| include_experiment_projects | query | boolean | Include experiment projects in the response. Experiment projects are created from running experiments. |
| include_dataset_evaluator_projects | query | boolean | Include dataset evaluator projects in the response. Dataset evaluator projects are created when running experiments with persisted evaluators. |

#### Responses
| Code | Description |
| --- | --- |
| 200 | A list of projects with pagination information |
| 403 | Forbidden |
| 422 | Unprocessable Content |

### Create a new project
`POST /v1/projects`

Create a new project with the specified configuration.

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | The newly created project |
| 403 | Forbidden |
| 422 | Unprocessable Content |

### Get project by ID or name
`GET /v1/projects/{project_identifier}`

Retrieve a specific project using its unique identifier: either project ID or project name. Note: When using a project name as the identifier, it cannot contain slash (/), question mark (?), or pound sign (#) characters.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| project_identifier | path | string | The project identifier: either project ID or project name. If using a project name, it cannot contain slash (/), question mark (?), or pound sign (#) characters. |

#### Responses
| Code | Description |
| --- | --- |
| 200 | The requested project |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

### Update a project by ID or name
`PUT /v1/projects/{project_identifier}`

Update an existing project with new configuration. Project names cannot be changed. The project identifier is either project ID or project name. Note: When using a project name as the identifier, it cannot contain slash (/), question mark (?), or pound sign (#) characters.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| project_identifier | path | string | The project identifier: either project ID or project name. If using a project name, it cannot contain slash (/), question mark (?), or pound sign (#) characters. |

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | The updated project |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

### Delete a project by ID or name
`DELETE /v1/projects/{project_identifier}`

Delete an existing project and all its associated data. The project identifier is either project ID or project name. The default project cannot be deleted. Note: When using a project name as the identifier, it cannot contain slash (/), question mark (?), or pound sign (#) characters.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| project_identifier | path | string | The project identifier: either project ID or project name. If using a project name, it cannot contain slash (/), question mark (?), or pound sign (#) characters. |

#### Responses
| Code | Description |
| --- | --- |
| 204 | No content returned on successful deletion |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

## Prompts

### List all prompts
`GET /v1/prompts`

Retrieve a paginated list of all prompts in the system. A prompt can have multiple versions.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| cursor | query | string | Cursor for pagination (base64-encoded prompt ID) |
| limit | query | integer | The max number of prompts to return at a time. |

#### Responses
| Code | Description |
| --- | --- |
| 200 | A list of prompts with pagination information |
| 403 | Forbidden |
| 422 | Unprocessable Content |

### Create a new prompt
`POST /v1/prompts`

Create a new prompt and its initial version. A prompt can have multiple versions.

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | The newly created prompt version |
| 403 | Forbidden |
| 422 | Unprocessable Content |

### List prompt versions
`GET /v1/prompts/{prompt_identifier}/versions`

Retrieve all versions of a specific prompt with pagination support. Each prompt can have multiple versions with different configurations.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| prompt_identifier | path | string | The identifier of the prompt, i.e. name or ID. |
| cursor | query | string | Cursor for pagination (base64-encoded promptVersion ID) |
| limit | query | integer | The max number of prompt versions to return at a time. |

#### Responses
| Code | Description |
| --- | --- |
| 200 | A list of prompt versions with pagination information |
| 403 | Forbidden |
| 422 | Unprocessable Content |
| 404 | Not Found |

### Get prompt version by ID
`GET /v1/prompt_versions/{prompt_version_id}`

Retrieve a specific prompt version using its unique identifier. A prompt version contains the actual template and configuration.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| prompt_version_id | path | string | The ID of the prompt version. |

#### Responses
| Code | Description |
| --- | --- |
| 200 | The requested prompt version |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

### Get prompt version by tag
`GET /v1/prompts/{prompt_identifier}/tags/{tag_name}`

Retrieve a specific prompt version using its tag name. Tags are used to identify specific versions of a prompt.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| prompt_identifier | path | string | The identifier of the prompt, i.e. name or ID. |
| tag_name | path | string | The tag of the prompt version |

#### Responses
| Code | Description |
| --- | --- |
| 200 | The prompt version with the specified tag |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

### Get latest prompt version
`GET /v1/prompts/{prompt_identifier}/latest`

Retrieve the most recent version of a specific prompt.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| prompt_identifier | path | string | The identifier of the prompt, i.e. name or ID. |

#### Responses
| Code | Description |
| --- | --- |
| 200 | The latest version of the specified prompt |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

### List prompt version tags
`GET /v1/prompt_versions/{prompt_version_id}/tags`

Retrieve all tags associated with a specific prompt version. Tags are used to identify and categorize different versions of a prompt.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| prompt_version_id | path | string | The ID of the prompt version. |
| cursor | query | string | Cursor for pagination (base64-encoded promptVersionTag ID) |
| limit | query | integer | The max number of tags to return at a time. |

#### Responses
| Code | Description |
| --- | --- |
| 200 | A list of tags associated with the prompt version |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

### Add tag to prompt version
`POST /v1/prompt_versions/{prompt_version_id}/tags`

Add a new tag to a specific prompt version. Tags help identify and categorize different versions of a prompt.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| prompt_version_id | path | string | The ID of the prompt version. |

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 204 | No content returned on successful tag creation |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

### Delete a tag from a prompt version
`DELETE /v1/prompt_versions/{prompt_version_id}/tags/{tag_name}`

Delete a tag from a specific prompt version by tag name. The tag is resolved within the scope of the prompt linked to the version.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| prompt_version_id | path | string | The ID of the prompt version. |
| tag_name | path | string | The name of the tag to delete. |

#### Responses
| Code | Description |
| --- | --- |
| 204 | No content returned on successful tag deletion |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

### Delete a prompt
`DELETE /v1/prompts/{prompt_identifier}`

Delete a prompt and all its versions, tags, and labels by identifier.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| prompt_identifier | path | string | The identifier of the prompt, i.e. name or ID. |

#### Responses
| Code | Description |
| --- | --- |
| 204 | Successful Response |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

## Secrets

### Upsert or delete secrets
`PUT /v1/secrets`

Atomically upsert or delete a batch of secrets. Entries with a non-null `value` are created or updated; entries with `value: null` are deleted. The `value` field is required for every entry, and omitting it returns 422. When the same key appears more than once, the last occurrence wins. Deleting a non-existent key succeeds silently. Secret values are never returned in the response.

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 422 | Unprocessable Content |
| 507 | Insufficient Storage |

## Sessions

### Get session by ID or session_id
`GET /v1/sessions/{session_identifier}`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| session_identifier | path | string | The session identifier: either a GlobalID or user-provided session_id string. |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

### Delete a session by identifier
`DELETE /v1/sessions/{session_identifier}`

Delete a session by its identifier. The identifier can be either:
1. A global ID (base64-encoded)
2. A user-provided session_id string

This will permanently remove the session and all associated traces, spans, and annotations via cascade delete.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| session_identifier | path | string | The session identifier: either a GlobalID or user-provided session_id string. |

#### Responses
| Code | Description |
| --- | --- |
| 204 | Successful Response |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Validation Error |

### Bulk delete sessions
`POST /v1/sessions/delete`

Delete multiple sessions by their identifiers (GlobalIDs or session_id strings). All identifiers in a single request must be the same type. Non-existent IDs are silently skipped. All associated traces, spans, and annotations are cascade deleted.

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 204 | Successful Response |
| 403 | Forbidden |
| 422 | Unprocessable Content |

### List sessions for a project
`GET /v1/projects/{project_identifier}/sessions`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| project_identifier | path | string | The project identifier: either project ID or project name. |
| cursor | query | string | Cursor for pagination (session ID) |
| limit | query | integer | The max number of sessions to return at a time. |
| order | query | string | Sort order by ID: 'asc' (ascending) or 'desc' (descending). |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

### Create session annotations
`POST /v1/session_annotations`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| sync | query | boolean | If true, fulfill request synchronously. |

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Session annotations inserted successfully |
| 403 | Forbidden |
| 404 | Session not found |
| 422 | Validation Error |

### Create a session note
`POST /v1/session_notes`

Add a note annotation to a session. Each call appends a new note with an auto-generated UUIDv4 identifier, so multiple notes accumulate on the same session. Structured annotations, by contrast, are keyed by (name, session_id, identifier) — re-writing the same key overwrites the existing annotation, so to keep multiple structured annotations with the same name on a session you must supply distinct identifiers.

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Session note created successfully |
| 403 | Forbidden |
| 404 | Session not found |
| 422 | Validation Error |

## Spans

### Search spans with simple filters (no DSL)
`GET /v1/projects/{project_identifier}/spans/otlpv1`

Return spans within a project filtered by time range. Supports cursor-based pagination.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| project_identifier | path | string | The project identifier: either project ID or project name. If using a project name, it cannot contain slash (/), question mark (?), or pound sign (#) characters. |
| cursor | query | string | Pagination cursor (Span Global ID) |
| limit | query | integer | Maximum number of spans to return |
| start_time | query | string | Inclusive lower bound time |
| end_time | query | string | Exclusive upper bound time |
| trace_id | query | array | Filter by one or more trace IDs |
| parent_id | query | string | Filter by parent span ID. Use "null" to get root spans only. |
| name | query | array | Filter by span name(s) |
| status_code | query | array | Filter by status code(s). Values: OK, ERROR, UNSET |
| attribute | query | array | Filter spans by `key:value`. Key is a dot-path (e.g. `user.id`, `metadata.tier`). Value is JSON-parsed: `k:12345` is int, `k:true` is bool, otherwise string (`k:user-42`). To match a numeric- or boolean-looking STRING, JSON-quote it: `user.id:"12345"` (URL-encoded `%2212345%22`). Split is on the first `:` only, so values may contain colons (`session.id:sess:abc:123`, ISO timestamps). Repeat the param to AND filters. List-valued attributes (e.g. `tag.tags`) cannot be matched here. Returns 422 on malformed input (missing colon, empty key/value, or list/dict/null value). |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

### List spans with simple filters (no DSL)
`GET /v1/projects/{project_identifier}/spans`

Return spans within a project filtered by time range. Supports cursor-based pagination.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| project_identifier | path | string | The project identifier: either project ID or project name. If using a project name, it cannot contain slash (/), question mark (?), or pound sign (#) characters. |
| cursor | query | string | Pagination cursor (Span Global ID) |
| limit | query | integer | Maximum number of spans to return |
| start_time | query | string | Inclusive lower bound time |
| end_time | query | string | Exclusive upper bound time |
| trace_id | query | array | Filter by one or more trace IDs |
| parent_id | query | string | Filter by parent span ID. Use "null" to get root spans only. |
| name | query | array | Filter by span name(s) |
| span_kind | query | array | Filter by span kind(s). Values: LLM, CHAIN, TOOL, RETRIEVER, EMBEDDING, AGENT, RERANKER, GUARDRAIL, EVALUATOR, UNKNOWN |
| status_code | query | array | Filter by status code(s). Values: OK, ERROR, UNSET |
| attribute | query | array | Filter spans by `key:value`. Key is a dot-path (e.g. `user.id`, `metadata.tier`). Value is JSON-parsed: `k:12345` is int, `k:true` is bool, otherwise string (`k:user-42`). To match a numeric- or boolean-looking STRING, JSON-quote it: `user.id:"12345"` (URL-encoded `%2212345%22`). Split is on the first `:` only, so values may contain colons (`session.id:sess:abc:123`, ISO timestamps). Repeat the param to AND filters. List-valued attributes (e.g. `tag.tags`) cannot be matched here. Returns 422 on malformed input (missing colon, empty key/value, or list/dict/null value). |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

### Create spans
`POST /v1/projects/{project_identifier}/spans`

Submit spans to be inserted into a project. If any spans are invalid or duplicates, no spans will be inserted.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| project_identifier | path | string | The project identifier: either project ID or project name. If using a project name, it cannot contain slash (/), question mark (?), or pound sign (#) characters. |

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 202 | Successful Response |
| 403 | Forbidden |
| 404 | Not Found |
| 400 | Bad Request |
| 422 | Validation Error |

### Create span annotations
`POST /v1/span_annotations`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| sync | query | boolean | If true, fulfill request synchronously. |

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Span annotations inserted successfully |
| 403 | Forbidden |
| 404 | Span not found |
| 422 | Validation Error |

### Create a span note
`POST /v1/span_notes`

Add a note annotation to a span. Each call appends a new note with an auto-generated UUIDv4 identifier, so multiple notes accumulate on the same span. Structured annotations, by contrast, are keyed by (name, span_id, identifier) — re-writing the same key overwrites the existing annotation, so to keep multiple structured annotations with the same name on a span you must supply distinct identifiers.

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Span note created successfully |
| 403 | Forbidden |
| 404 | Span not found |
| 422 | Validation Error |

### Delete a span by span_identifier
`DELETE /v1/spans/{span_identifier}`

Delete a single span by identifier.

        **Important**: This operation deletes ONLY the specified span itself and does NOT
        delete its descendants/children. All child spans will remain in the trace and
        become orphaned (their parent_id will point to a non-existent span).

        Behavior:
        - Deletes only the target span (preserves all descendant spans)
        - If this was the last span in the trace, the trace record is also deleted
        - If the deleted span had a parent, its cumulative metrics (error count, token counts)
          are subtracted from all ancestor spans in the chain

        **Note**: This operation is irreversible and may create orphaned spans.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| span_identifier | path | string | The span identifier: either a relay GlobalID or OpenTelemetry span_id |

#### Responses
| Code | Description |
| --- | --- |
| 204 | Successful Response |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Validation Error |

### Annotate Span Documents
`POST /v1/document_annotations`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| sync | query | boolean | If set to true, the annotations are inserted synchronously. |

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Span document annotation inserted successfully |
| 403 | Forbidden |
| 404 | Span not found |
| 422 | Invalid request - non-empty identifier not supported |

## Traces

### List traces for a project
`GET /v1/projects/{project_identifier}/traces`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| project_identifier | path | string | The project identifier: either project ID or project name. |
| start_time | query | string | Inclusive lower bound on trace start time (ISO 8601) |
| end_time | query | string | Exclusive upper bound on trace start time (ISO 8601) |
| sort | query | string | Sort field |
| order | query | string | Sort direction |
| limit | query | integer | Maximum number of traces to return |
| cursor | query | string | Pagination cursor (Trace GlobalID) |
| include_spans | query | boolean | If true, include full span details for each trace. This significantly increases response size and query latency, especially with large page sizes. Prefer fetching spans lazily for individual traces when possible. |
| session_identifier | query | array | List of session identifiers to filter traces by. Each value can be either a session_id string or a session GlobalID. Only traces belonging to the specified sessions will be returned. |

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Unprocessable Content |

### Create trace annotations
`POST /v1/trace_annotations`

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| sync | query | boolean | If true, fulfill request synchronously. |

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Successful Response |
| 403 | Forbidden |
| 404 | Trace not found |
| 422 | Validation Error |

### Create a trace note
`POST /v1/trace_notes`

Add a note annotation to a trace. Each call appends a new note with an auto-generated UUIDv4 identifier, so multiple notes accumulate on the same trace. Structured annotations, by contrast, are keyed by (name, trace_id, identifier) — re-writing the same key overwrites the existing annotation, so to keep multiple structured annotations with the same name on a trace you must supply distinct identifiers.

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 200 | Trace note created successfully |
| 403 | Forbidden |
| 404 | Trace not found |
| 422 | Validation Error |

### Delete a trace by identifier
`DELETE /v1/traces/{trace_identifier}`

Delete an entire trace by its identifier. The identifier can be either:
1. A Relay node ID (base64-encoded)
2. An OpenTelemetry trace_id (hex string)

This will permanently remove all spans in the trace and their associated data.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| trace_identifier | path | string | The trace identifier: either a relay GlobalID or OpenTelemetry trace_id |

#### Responses
| Code | Description |
| --- | --- |
| 204 | Successful Response |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Validation Error |

## Users

### Get the authenticated user
`GET /v1/user`

Returns the profile of the currently authenticated user. When authentication is disabled, returns an anonymous user representation.

#### Responses
| Code | Description |
| --- | --- |
| 200 | The authenticated user's profile. |
| 403 | Forbidden |
| 401 | User not found. |

### List all users
`GET /v1/users`

Retrieve a paginated list of all users in the system.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| cursor | query | string | Cursor for pagination (base64-encoded user ID) |
| limit | query | integer | The max number of users to return at a time. |

#### Responses
| Code | Description |
| --- | --- |
| 200 | A list of users. |
| 403 | Forbidden |
| 422 | Unprocessable Content |

### Create a new user
`POST /v1/users`

Create a new user with the specified configuration.

#### Request Body
- Content-Type: `application/json`

#### Responses
| Code | Description |
| --- | --- |
| 201 | The newly created user. |
| 403 | Forbidden |
| 400 | Role not found. |
| 409 | Username or email already exists. |
| 422 | Unprocessable Content |

### Delete a user by ID
`DELETE /v1/users/{user_id}`

Delete an existing user by their unique GlobalID.

#### Parameters
| Name | In | Type | Description |
| --- | --- | --- | --- |
| user_id | path | string | The GlobalID of the user (e.g. 'VXNlcjox'). |

#### Responses
| Code | Description |
| --- | --- |
| 204 | No content returned on successful deletion. |
| 403 | Cannot delete the default admin or system user |
| 404 | User not found. |
| 422 | Unprocessable Content |
