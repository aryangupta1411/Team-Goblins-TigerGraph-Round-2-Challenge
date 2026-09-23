# Social posts: final copy

Tag @TigerGraphDB and link the blog in every version. Replace `<BLOG_URL>`, `<DEMO_URL>` and `<REPO_URL>` before posting.

## X: single post

> A $74.96 purchase, bank risk score 0.05. My agent on @TigerGraphDB followed its device to 52 cards and 4 confirmed-fraud cases, drafted a SAR, and sent it and the card block to humans. The graph and a deterministic engine decide; the LLM investigates. <BLOG_URL>

Length: 262 characters as written. X counts any link as 23 characters, so the post is 275 once the real URL goes in. Keep the limit in mind if you edit it.

## X: optional thread (4 posts)

**1/4** (268 characters with the link)
> A $74.96 purchase, bank risk score 0.05. My fraud agent on @TigerGraphDB followed its device to 52 cards and 4 confirmed-fraud cases. The graph and a deterministic engine decide; the LLM investigates and explains. 20 alerts ran live end to end. <BLOG_URL>

**2/4** (233)
> The LLM reaches the graph only through the official TigerGraph MCP server, as 16 typed tools over my GSQL queries, time-bounded to when the case opened. It can move the engine's fraud probability by at most 0.10, with a cited reason.

**3/4** (271)
> Honest negative result: WCC over shared devices gave one 3,687-card component, so it can't find rings here. A profile-centric rule does: one anonymous-proxy device profile, 52 cards, 4 confirmed fraud cases. That's HHG-014, which ended with a SAR drafted for L2 sign-off.

**4/4** (269 with both links)
> Also honest: 20/20 agreement with my offline engine is a self-consistency check, not accuracy. Replaying 310 real closed cases, pattern accuracy on the 180 confirmed-fraud ones was 0.88 vs my 0.95 target. Write-up + demo: <BLOG_URL> <DEMO_URL>

## LinkedIn

> A $74.96 online purchase. Bank risk score: 0.05. A model that only scored transactions would let it through.
>
> My agent followed that purchase's device through the graph to 52 cards, 114 anonymous-proxy uses and four earlier confirmed-fraud cases. It drafted a suspicious activity report and sent the card block and the filing to human approvers.
>
> I built it for the TigerGraph x Hacker House Goa 2026 hackathon on @TigerGraph.
>
> An alert comes in: a bank risk score, a customer dispute or an analyst request. The agent investigates through typed GSQL queries exposed via the official TigerGraph MCP server. It retrieves precedents from 5,565 closed cases and grounds itself in FinCEN, FFIEC and FATF guidance, all stored in TigerVector. It asks the customer only when the answer could change the action. It writes the case back into the graph.
>
> The design choice I'd reuse: the graph and a deterministic engine make the decisions, and the LLM investigates and explains. It can move the fraud probability by at most 0.10, with a cited reason. Every block or SAR goes to a human approver under a policy-as-code gate.
>
> One lesson: connected components over shared devices gave a single 3,687-card component, so rings had to be found profile by profile.
>
> All 20 benchmark alerts ran live, end to end. They agreed 20/20 with my own offline engine, but that is a self-consistency check, not accuracy: on a replay of 310 real closed cases, pattern accuracy on the 180 confirmed-fraud ones was 0.88 against my 0.95 target.
>
> Blog: <BLOG_URL>
> Demo: <DEMO_URL>
> Code: <REPO_URL>
>
> #TigerGraph #GraphRAG #FraudDetection #AgenticAI #HackerHouseGoa

On LinkedIn, type "@TigerGraph" and choose the company page from the mention picker so the tag links.

## Pre-posting checklist

- [ ] Publish `docs/blog.md` on dev.to or Hashnode (both render the tables). Put the H1 in the title field and the italic line in the subtitle field. Delete both lines and the `---` rule under them from the body. On Medium, the five tables must first be rebuilt as images or bullet lists.
- [ ] Replace `<DEMO_URL>` and `<REPO_URL>` at the end of the blog
- [ ] Upload the demo video and copy its URL
- [ ] Replace every `<BLOG_URL>`, `<DEMO_URL>` and `<REPO_URL>` above (repo: https://github.com/aryangupta1411/Tiger-graph)
- [ ] Re-check the X post is 280 characters or fewer after editing (a link counts as 23)
- [ ] Confirm @TigerGraphDB is tagged on X and the TigerGraph page is tagged on LinkedIn
- [ ] Paste the blog, demo and social-post URLs into the three "TODO link" rows in `README.md`
