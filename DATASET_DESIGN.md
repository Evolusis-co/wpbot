# Dataset Design for Course-Aware Workplace Bot

## Overview
The bot needs two data sources:
1. **Dataset (Static)** - Complete course content, scenarios, skills info (what you're creating now)
2. **Database (Dynamic)** - User progress, completed courses (will be implemented later)

## Dataset Structure

### Approach: Scenario-Based with Course Metadata

Each course should have:
- **Course metadata** (ID, title, skills covered, prerequisites)
- **Practical scenarios** (real workplace situations the course helps with)
- **Key concepts** (concise summaries, not full content)
- **Application tips** (how to use the skill at work)

This allows the bot to:
- Match user questions to relevant courses
- Provide context-aware guidance based on completed courses
- Suggest next courses based on workplace challenges

---

## Recommended Format

### Option 1: JSON (Structured, Easy to Query)

```json
{
  "courses": [
    {
      "course_id": "ADAPT_101",
      "title": "Adaptability Fundamentals",
      "category": "Adaptability & Flexibility",
      "skills": ["STEP Framework", "Change Management", "Flexibility"],
      "duration": "2 hours",
      "prerequisites": [],
      "scenarios": [
        {
          "scenario_id": "ADAPT_S1",
          "situation": "Manager keeps changing project deadlines last minute",
          "context": "You're frustrated because priorities shift constantly",
          "step_guidance": {
            "spot": "Identify that the core issue is external unpredictability vs your internal reaction",
            "think": "Consider: Is the change pattern, or one-off? What's within my control?",
            "engage": "Try: Set buffer time in personal planning, clarify priorities with manager weekly",
            "perform": "Reflect: Did the buffer help? Can I predict change patterns better?"
          },
          "key_phrases": ["changing deadlines", "priorities shift", "last minute changes", "unpredictable manager"]
        },
        {
          "scenario_id": "ADAPT_S2",
          "situation": "Assigned to use new software/tool with minimal training",
          "context": "Feeling overwhelmed by having to learn on the job",
          "step_guidance": {
            "spot": "The challenge is adapting to new tools under time pressure",
            "think": "What's the minimum I need to know vs nice-to-know? Who can help?",
            "engage": "Action: Dedicate 30min daily to learn basics, ask colleague for quick tips",
            "perform": "Check-in: Am I functional with the tool? What worked in learning?"
          },
          "key_phrases": ["new software", "new tool", "no training", "learn quickly", "overwhelmed by change"]
        }
      ],
      "key_concepts": [
        "STEP Framework: Spot the challenge → Think through options → Engage with one action → Perform and reflect",
        "Adaptability is about managing your response to change, not controlling the change itself",
        "Small, consistent actions build flexibility better than waiting for perfect conditions"
      ],
      "application_tips": [
        "Use STEP for any workplace change: new role, new team, new processes",
        "Practice 'What's in my control?' daily to reduce frustration",
        "Build flexibility muscle by trying one new approach per week"
      ]
    },
    {
      "course_id": "EQ_201",
      "title": "Emotional Intelligence at Work",
      "category": "Emotional Intelligence",
      "skills": ["4Rs Framework", "Self-Regulation", "Empathy", "Conflict Resolution"],
      "duration": "3 hours",
      "prerequisites": [],
      "scenarios": [
        {
          "scenario_id": "EQ_S1",
          "situation": "Colleague takes credit for your idea in a meeting",
          "context": "Feeling angry and undervalued, unsure how to respond professionally",
          "4rs_guidance": {
            "recognize": "Notice: anger, hurt, impulse to call them out publicly",
            "regulate": "Pause before reacting. Breathe. Decide: address privately first",
            "respect": "Consider: Maybe they didn't realize, or felt pressure. Approach with curiosity",
            "reflect": "After conversation: Did staying calm help? What boundary can I set?"
          },
          "key_phrases": ["took credit", "my idea", "not recognized", "feeling angry", "undervalued"]
        },
        {
          "scenario_id": "EQ_S2",
          "situation": "Received harsh feedback from manager in front of team",
          "context": "Embarrassed and defensive, struggling to process the feedback objectively",
          "4rs_guidance": {
            "recognize": "Emotions: embarrassment, defensiveness, hurt pride",
            "regulate": "Don't respond immediately. Take a break. Separate feedback from delivery",
            "respect": "Acknowledge: feedback may be valid even if delivery was poor. Manager may be stressed",
            "reflect": "Later: What was true in the feedback? How can I request private feedback going forward?"
          },
          "key_phrases": ["harsh feedback", "public criticism", "embarrassed", "defensive", "criticized in front of team"]
        }
      ],
      "key_concepts": [
        "4Rs Framework: Recognize emotions → Regulate your response → Respect others' perspectives → Reflect on outcomes",
        "Emotional intelligence is noticing emotions and choosing your response, not suppressing feelings",
        "High EQ means separating what happened from how it was delivered"
      ],
      "application_tips": [
        "Use 4Rs in any conflict or emotionally charged situation",
        "Practice recognizing emotions throughout the day: 'What am I feeling right now?'",
        "Before difficult conversations, pre-plan how you'll regulate if triggered"
      ]
    }
  ]
}
```

### Option 2: Markdown (Human-Readable, Easy to Edit)

```markdown
---
course_id: ADAPT_101
title: Adaptability Fundamentals
category: Adaptability & Flexibility
skills: [STEP Framework, Change Management, Flexibility]
duration: 2 hours
prerequisites: []
---

## Course Overview
Teaches the STEP framework for handling workplace changes and building flexibility.

## Scenarios

### Scenario 1: Constantly Changing Deadlines
**Situation:** Manager keeps changing project deadlines last minute  
**Context:** You're frustrated because priorities shift constantly  
**Key Phrases:** changing deadlines, priorities shift, last minute changes, unpredictable manager

**STEP Guidance:**
- **Spot:** Identify that the core issue is external unpredictability vs your internal reaction
- **Think:** Consider: Is the change pattern, or one-off? What's within my control?
- **Engage:** Try: Set buffer time in personal planning, clarify priorities with manager weekly
- **Perform:** Reflect: Did the buffer help? Can I predict change patterns better?

### Scenario 2: Learning New Tools on the Fly
**Situation:** Assigned to use new software/tool with minimal training  
**Context:** Feeling overwhelmed by having to learn on the job  
**Key Phrases:** new software, new tool, no training, learn quickly, overwhelmed by change

**STEP Guidance:**
- **Spot:** The challenge is adapting to new tools under time pressure
- **Think:** What's the minimum I need to know vs nice-to-know? Who can help?
- **Engage:** Action: Dedicate 30min daily to learn basics, ask colleague for quick tips
- **Perform:** Check-in: Am I functional with the tool? What worked in learning?

## Key Concepts
- STEP Framework: Spot → Think → Engage → Perform
- Adaptability is about managing your response to change, not controlling the change itself
- Small, consistent actions build flexibility better than waiting for perfect conditions

## Application Tips
- Use STEP for any workplace change: new role, new team, new processes
- Practice 'What's in my control?' daily to reduce frustration
- Build flexibility muscle by trying one new approach per week
```

---

## Data Structure Components Explained

### 1. Course Metadata
- `course_id`: Unique identifier (e.g., ADAPT_101, EQ_201)
- `title`: Human-readable name
- `category`: Broad topic (Adaptability, Emotional Intelligence, Communication, etc.)
- `skills`: Specific skills taught (for matching to user queries)
- `duration`: Estimated completion time
- `prerequisites`: Other courses needed first (empty array if none)

### 2. Scenarios (Most Important!)
Real workplace situations where the course content applies. Each scenario has:
- `scenario_id`: Unique ID
- `situation`: Brief description (1 sentence)
- `context`: Emotional/workplace context
- `key_phrases`: Search terms users might type (for matching user questions)
- `step_guidance` or `4rs_guidance`: Framework-specific advice

**Why scenarios?** Users won't ask "teach me STEP framework" — they'll say "my manager keeps changing deadlines." Scenarios bridge the gap.

### 3. Key Concepts
Concise summaries (2-3 sentences each) of core ideas. NOT full course text — just enough for the bot to explain the concept in context.

### 4. Application Tips
Practical how-to-use advice. Helps bot give actionable guidance even if user hasn't completed the course.

---

## Bot Behavior Logic (For Later Implementation)

### When User Asks Question:

1. **Match to scenario** using key_phrases
2. **Check database:** Has user completed this course?
   - ✅ **Yes** → Provide full STEP/4Rs guidance from scenario
   - ❌ **No** → Provide light guidance + suggest: "This is exactly what we cover in [Course Title]. Want to unlock this course?"

### Example Flow:

**User:** "My manager keeps changing deadlines and I'm so frustrated"

**Bot checks:**
- Matches to `ADAPT_S1` scenario
- Queries database: `SELECT completed FROM user_courses WHERE user_id=X AND course_id='ADAPT_101'`

**If course completed:**
```
Let's use the STEP framework you learned:

Spot: The core challenge is external unpredictability vs your internal reaction.
Think: Is this a pattern or one-off? What's in your control?
Engage: Try setting buffer time in your planning. Can you clarify priorities with your manager weekly?
Perform: After trying this, reflect—did the buffer help?
```

**If course NOT completed:**
```
That's super frustrating. Sounds like you're dealing with constant change, which is tough.

The main thing in your control? How you plan around the uncertainty. Could you build buffer time into your personal deadlines?

💡 This is exactly what we cover in "Adaptability Fundamentals" — the STEP framework for handling workplace changes. Want to start that course?
```

---

## Recommended Next Steps

1. **Start with 2-3 courses** you currently support (Adaptability, Emotional Intelligence)
2. **Create 5-8 scenarios per course** (cover common workplace issues)
3. **Use JSON format** (easier to integrate with database later)
4. **Test with bot:** Add to vector store, see if bot matches user questions to scenarios correctly
5. **Expand gradually:** Add more courses as you build content

---

## File Naming Convention

- `course_data.json` - Single file with all courses
- OR `courses/ADAPT_101.json`, `courses/EQ_201.json` - One file per course

---

## Integration with Current Bot

Current bot uses:
- Qdrant vector store (currently has "bridgetext_scenarios" collection)
- Google embeddings
- LangChain retrieval

**To add course data:**
1. Create JSON with format above
2. Use existing `upload_to_qdrant.py` (or similar) to embed scenarios
3. Bot will automatically match user questions to scenarios via semantic search
4. Later: Add database check before providing full guidance

---

## Summary

**Dataset should contain:**
- ✅ Course metadata (ID, title, skills, category)
- ✅ Practical scenarios (real workplace situations)
- ✅ Key phrases (for matching user questions)
- ✅ Framework guidance (STEP/4Rs instructions)
- ✅ Key concepts (concise summaries)
- ✅ Application tips (how to use the skill)

**Dataset should NOT contain:**
- ❌ Full course videos/text/lectures
- ❌ Quiz questions (unless for practice)
- ❌ User progress (that's in database)

**Recommended format:** JSON for structure, scenarios as core content.
