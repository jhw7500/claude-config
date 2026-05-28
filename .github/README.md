# GitHub Actions Configuration

This directory contains GitHub Actions workflows for automated code review and quality assurance using Claude Code and Gemini.

## Workflows

### Claude Code Workflows

#### 1. `claude.yml` - Interactive Claude Code Assistant
Triggers when `@claude` is mentioned in:
- Issue comments
- Pull request review comments
- Issue descriptions
- Pull request reviews

**Required Secret:**
- `CLAUDE_CODE_OAUTH_TOKEN` - OAuth token from https://claude.com/code/oauth

**Usage:**
```
@claude Please review this code
@claude Explain the changes in this PR
@claude Fix the bug described in this issue
```

#### 2. `claude-code-review.yml` - Automatic PR Review
Automatically reviews pull requests when opened or updated.

**Required Secret:**
- `CLAUDE_CODE_OAUTH_TOKEN`

**Features:**
- Code quality analysis
- Bug detection
- Performance considerations
- Security concerns
- Test coverage assessment

### Gemini Workflows

#### 1. `gemini-auto-review.yml` - Automatic PR Review ⭐ NEW
**Automatically reviews pull requests when opened or updated** (similar to Claude Code Review).

**Required Secret:**
- `GEMINI_API_KEY` - API key from Google AI Studio

**Features:**
- Overall code assessment
- Critical issue detection (security, bugs, breaking changes)
- Code quality suggestions
- Performance considerations
- Uses Gemini 2.0 Flash (Experimental) model

**Triggers:** Automatically on PR opened or synchronized (new commits pushed)

#### 2. `gemini-dispatch.yml` - Gemini Request Dispatcher
Central dispatcher for routing Gemini-related requests (requires `@gemini-cli` mention).

#### 3. `gemini-invoke.yml` - Direct Gemini Invocation
Handles direct Gemini CLI invocations for code analysis.

#### 4. `gemini-review.yml` - Gemini PR Review
Provides Gemini-powered code reviews on pull requests (called by dispatch).

#### 5. `gemini-triage.yml` - Issue Triage
Automatically triages and categorizes issues using Gemini.

#### 6. `gemini-scheduled-triage.yml` - Scheduled Issue Analysis
Runs periodic analysis of repository issues.

**Required Secrets:**
- `GEMINI_API_KEY` - API key from Google AI Studio
- `GCP_PROJECT_ID` - Google Cloud Project ID (if using Vertex AI)
- `GCP_LOCATION` - GCP region (e.g., `us-central1`)

**Required Variables:**
- Additional configuration may be required in workflow files

## Setup Instructions

### 1. Claude Code Setup

1. Go to https://claude.com/code/oauth
2. Generate an OAuth token for your repository
3. Add the token as a repository secret:
   - Go to repository **Settings** → **Secrets and variables** → **Actions**
   - Click **New repository secret**
   - Name: `CLAUDE_CODE_OAUTH_TOKEN`
   - Value: (paste your token)

### 2. Gemini Auto Review Setup (Recommended)

For automatic PR reviews with Gemini (similar to Claude):

1. Get a Gemini API key:
   - Visit https://aistudio.google.com/app/apikey
   - Create a new API key or use existing one

2. Add the API key as a repository secret:
   - Go to repository **Settings** → **Secrets and variables** → **Actions**
   - Click **New repository secret**
   - Name: `GEMINI_API_KEY`
   - Value: (paste your API key)

3. That's it! The `gemini-auto-review.yml` workflow will now automatically review PRs.

### 3. Advanced Gemini Setup (Optional)

For advanced Gemini features (dispatch, triage, scheduled reviews):

1. Get a Gemini API key (same as above)

2. (Optional) Set up Google Cloud Project for Vertex AI:
   - Create a GCP project at https://console.cloud.google.com
   - Enable Vertex AI API
   - Note your project ID and preferred region

3. Add additional secrets to your repository:
   - `GEMINI_API_KEY` - Your Gemini API key (required)
   - `GCP_PROJECT_ID` - (if using Vertex AI)
   - `GCP_LOCATION` - (if using Vertex AI, e.g., `us-central1`)

### 4. Verify Setup

After adding secrets, create a test PR or mention `@claude` in an issue to verify the workflows are working.

**Note:** Both Claude and Gemini auto-review workflows will run automatically on new PRs once their respective secrets are configured.

## Workflow Permissions

All workflows require these permissions:
- `contents: read` - Read repository contents
- `pull-requests: read` - Read PR information
- `issues: read` - Read issue information
- `id-token: write` - Generate OIDC tokens (for Claude)
- `actions: read` - Read CI results (for Claude PR reviews)

## Disabling Workflows

To disable a workflow:
1. Go to **Actions** tab in your repository
2. Select the workflow you want to disable
3. Click the **⋯** menu
4. Select **Disable workflow**

Or delete/rename the workflow file in `.github/workflows/`.

## Customization

### Adjusting Claude Prompts

Edit the `prompt` parameter in `claude-code-review.yml`:

```yaml
prompt: |
  Please review this pull request focusing on:
  - Your custom criteria here
  - Additional focus areas
```

### Filtering Claude Reviews by Author

Uncomment and modify the `if` condition in `claude-code-review.yml`:

```yaml
if: |
  github.event.pull_request.user.login == 'external-contributor' ||
  github.event.pull_request.author_association == 'FIRST_TIME_CONTRIBUTOR'
```

### Adjusting Gemini Configuration

Edit the respective `gemini-*.yml` files to modify:
- Trigger conditions
- API endpoints
- Analysis parameters
- Output formats

## Troubleshooting

### Claude workflows are skipped
- Verify `CLAUDE_CODE_OAUTH_TOKEN` is set correctly
- Check if the trigger condition matches (e.g., `@claude` mention)
- Review workflow run logs in the **Actions** tab

### Gemini workflows fail
- Verify `GEMINI_API_KEY` is valid and not expired
- Check API quota limits at https://aistudio.google.com
- Ensure GCP credentials are correct (if using Vertex AI)

### Workflows don't trigger
- Check branch protection rules
- Verify workflow file syntax (YAML formatting)
- Ensure required permissions are granted

## Cost Considerations

- **Claude Code**: Usage is metered through your Anthropic account
- **Gemini**: Free tier available, check https://ai.google.dev/pricing for limits
- Review usage regularly to avoid unexpected costs

## Security Notes

- Never commit secrets directly to workflow files
- Use repository secrets for all sensitive data
- Regularly rotate API keys and tokens
- Review workflow logs for sensitive data exposure
- Limit workflow permissions to minimum required

## Support

- **Claude Code**: https://code.claude.com/docs
- **Gemini**: https://ai.google.dev/docs
- **GitHub Actions**: https://docs.github.com/actions

## Version History

- **2026-01-14**: Initial workflow setup with Claude and Gemini integration
