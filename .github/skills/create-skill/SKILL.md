---
name: create-skill
description: "Create a reusable skill (SKILL.md) from a workflow, checklist, or repeatable process. Use when: packaging a debugging method, turning a multi-step playbook into a reusable agent workflow, or formalizing a project-specific process for future use."
argument-hint: "What workflow should become a reusable skill?"
disable-model-invocation: false
---

Related skill: `agent-customization`. Load and follow the project guidance for customization file patterns, placement, and frontmatter requirements.

Guide the user to create a `SKILL.md` that captures a repeatable workflow in a reusable, discoverable way.

## Goal

Turn a real, recurring process into a concise skill that future agent sessions can invoke with clear instructions, decision points, and completion checks.

## Extract from Conversation

Review the conversation and identify the actual workflow being followed. Generalize it into a portable skill by capturing:

- The step-by-step process being followed
- Decision points and branching logic
- Quality criteria or completion checks
- Conditions for when the workflow applies or should stop
- Any required clarifying questions or assumptions

Look for patterns such as:
- investigation and root-cause analysis
- validation and verification steps
- checklist-based completion criteria
- scope or environment branching
- when to ask clarifying questions instead of proceeding

## Clarify if Needed

If the workflow is not yet clear, ask targeted questions before drafting the skill:

- What outcome should this skill produce?
- Is it meant for a workspace-only workflow or a personal workflow?
- Is the workflow a quick checklist or a full multi-step process?
- Are there key triggers, edge cases, or exit conditions to include?

If the user has not provided enough detail, refine the scope before writing the skill.

## Draft the Skill

Create the skill file in the appropriate customization location:

- Workspace-shared workflow: `.github/skills/<name>/SKILL.md`
- Personal workflow: `{{VSCODE_USER_PROMPTS_FOLDER}}/` if applicable

Include standard frontmatter:

- `name`: short, stable identifier matching the folder/project name
- `description`: strong discovery text that states when to use the skill
- `argument-hint`: helpful prompt for expected input
- `disable-model-invocation`: set to `false` unless the skill should not invoke models

Write the body with these sections:

1. Purpose or goal
2. When to use it
3. Step-by-step workflow
4. Decision points and branching logic
5. Quality checks and completion criteria
6. Example prompts or usage patterns
7. Follow-on customizations or related files

## Validate the Skill

Before finalizing, confirm the skill:

- Is saved in the correct location
- Has valid YAML frontmatter
- Uses a clear, discoverable description
- Captures a real workflow rather than generic advice
- Includes explicit quality gates or completion checks
- Makes ambiguous decisions actionable

## Iterate

If the draft is weak or too vague:

1. Identify the least clear section
2. Ask the user for missing detail or constraints
3. Tighten the workflow into concrete steps and decisions
4. Refine the description so the skill is easy to discover
5. Re-run validation and improve completion criteria

## Final Output

Once the skill is finalized, summarize:

- What the skill produces
- What workflow it packages
- Example prompts that would trigger or use it
- Related customization candidates to create next, such as prompts, instructions, or custom agents

## Example Prompt Patterns

Use prompts like:

- "Create a reusable skill for my debugging workflow."
- "Package this repeatable review checklist into a SKILL.md."
- "Turn our deployment verification steps into a reusable workflow skill."
- "Document this multi-step troubleshooting method as a project skill."

## Default Quality Bar

A strong skill should be:

- Specific enough to be useful
- Broad enough to be reusable
- Clear about when to apply it
- Actionable at each step
- Explicit about completion criteria
- Easy to discover through a strong description
