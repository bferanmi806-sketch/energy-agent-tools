"use client";

import { ChevronRight, Search, Sparkles, X } from "lucide-react";
import { useId, useMemo, useState } from "react";
import type { SkillsResponse } from "@energy-agent-tools/sdk";
import styles from "./SkillsCatalogue.module.css";

type Skill = SkillsResponse["skills"][number];

function searchText(skill: Skill): string {
  return [
    skill.id,
    skill.intent,
    ...skill.capabilities,
    ...skill.sequence,
    ...skill.pitfalls,
    ...(skill.supporting_tools ?? []),
    ...(skill.evidence_required ?? []),
    ...Object.entries(skill.parameters ?? {}).flatMap(([name, description]) => [name, description]),
  ].join(" ").toLocaleLowerCase();
}

function workflowCount(count: number): string {
  return `${count} ${count === 1 ? "workflow" : "workflows"}`;
}

function MetadataDetails({ skill }: { skill: Skill }) {
  const headingId = useId();
  const hasExecutable = skill.executable !== undefined;
  const hasEvidence = skill.evidence_required !== undefined;
  const hasParameters = skill.parameters !== undefined;

  if (!hasExecutable && !hasEvidence && !hasParameters) return null;

  const evidenceItems = skill.evidence_required ?? [];
  const parameterEntries = Object.entries(skill.parameters ?? {});

  return (
    <section className={`${styles.detailBlock} ${styles.detailBlockWide}`} aria-labelledby={headingId}>
      <h4 id={headingId}>Workflow metadata</h4>
      <dl className={styles.metadataFacts}>
        {hasExecutable ? <div><dt>Executable in gateway</dt><dd>{skill.executable ? "Yes" : "No"}</dd></div> : null}
        {hasEvidence ? (
          <div>
            <dt>Evidence requirements</dt>
            <dd>
              {evidenceItems.length > 0
                ? <ul className={styles.metadataBullets}>{evidenceItems.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul>
                : "No evidence details supplied."}
            </dd>
          </div>
        ) : null}
        {hasParameters ? (
          <div>
            <dt>Parameters</dt>
            <dd>
              {parameterEntries.length > 0 ? (
                <dl className={styles.parameterList}>
                  {parameterEntries.map(([name, value]) => (
                    <div key={name}>
                      <dt><code>{name}</code></dt>
                      <dd>{value}</dd>
                    </div>
                  ))}
                </dl>
              ) : "No parameter details supplied."}
            </dd>
          </div>
        ) : null}
      </dl>
    </section>
  );
}

export function SkillsCatalogue({ skills }: { skills: SkillsResponse["skills"] }) {
  const id = useId();
  const searchId = `${id}-search`;
  const resultsId = `${id}-results`;
  const detailId = `${id}-detail`;
  const [query, setQuery] = useState("");
  const [selectedSkillId, setSelectedSkillId] = useState<string | null>(() => skills[0]?.id ?? null);

  const filteredSkills = useMemo(() => {
    const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    if (terms.length === 0) return skills;
    return skills.filter((skill) => {
      const searchable = searchText(skill);
      return terms.every((term) => searchable.includes(term));
    });
  }, [query, skills]);

  const selectedSkill = filteredSkills.find((skill) => skill.id === selectedSkillId) ?? filteredSkills[0] ?? null;
  const count = filteredSkills.length === skills.length
    ? workflowCount(filteredSkills.length)
    : `${filteredSkills.length} of ${workflowCount(skills.length)}`;

  return (
    <section className="content-section" aria-labelledby={`${id}-heading`}>
      <div className="section-toolbar">
        <div>
          <h2 id={`${id}-heading`}>Supported energy questions</h2>
          <p>Your connected agent executes workflows. Use this catalogue to discover supported questions and inspect requirements supplied by the gateway.</p>
        </div>
        <span className="count-label" aria-live="polite">{count}</span>
      </div>

      <div className={styles.searchArea}>
        <label className={styles.searchLabel} htmlFor={searchId}>Search skills</label>
        <div className="search-field">
          <Search size={16} aria-hidden="true" />
          <input
            id={searchId}
            type="search"
            value={query}
            onChange={(event) => setQuery(event.currentTarget.value)}
            placeholder="Question, capability, tool, or workflow ID"
            aria-controls={resultsId}
          />
          {query ? (
            <button className={`clear-search ${styles.clearButton}`} type="button" onClick={() => setQuery("")} aria-label="Clear workflow search">
              <X size={15} aria-hidden="true" />
            </button>
          ) : null}
        </div>
      </div>

      <div id={resultsId}>
        {skills.length === 0 ? (
          <div className="empty-state compact-empty">
            <div className="empty-icon" aria-hidden="true"><Sparkles size={17} /></div>
            <h3>No workflow descriptions returned</h3>
            <p>This gateway returned no skill descriptions for the current identity.</p>
          </div>
        ) : filteredSkills.length === 0 ? (
          <div className="empty-state compact-empty" role="status" aria-live="polite">
            <div className="empty-icon" aria-hidden="true"><Search size={17} /></div>
            <h3>No questions match “{query.trim()}”</h3>
            <p>Try another phrase, capability, tool, or workflow ID.</p>
            <button className="text-button" type="button" onClick={() => setQuery("")}>Clear search</button>
          </div>
        ) : selectedSkill ? (
          <div className={styles.catalogueLayout}>
            <div className={styles.compactPicker}>
              <label htmlFor={`${id}-workflow`}>Choose a workflow</label>
              <select
                id={`${id}-workflow`}
                value={selectedSkill.id}
                aria-controls={detailId}
                onChange={(event) => setSelectedSkillId(event.currentTarget.value)}
              >
                {filteredSkills.map((skill) => <option key={skill.id} value={skill.id}>{skill.intent}</option>)}
              </select>
            </div>
            <nav className={styles.selector} aria-label="Matching supported workflows">
              <ul className={`${styles.choices} skill-list`}>
                {filteredSkills.map((skill) => {
                  const selected = skill.id === selectedSkill.id;
                  return (
                    <li key={skill.id}>
                      <button
                        className={`${styles.choice}${selected ? ` ${styles.choiceSelected}` : ""}`}
                        type="button"
                        aria-pressed={selected}
                        aria-controls={detailId}
                        onClick={() => setSelectedSkillId(skill.id)}
                      >
                        <span className={styles.choiceText}>
                          <span className={styles.choiceIntent}>{skill.intent}</span>
                          <span className="record-id">{skill.id}</span>
                        </span>
                        <ChevronRight className={styles.choiceArrow} size={16} aria-hidden="true" />
                      </button>
                    </li>
                  );
                })}
              </ul>
            </nav>

            <section className={styles.detailPanel} id={detailId} aria-labelledby={`${id}-detail-heading`}>
              <header className={styles.detailHeader}>
                <h3 id={`${id}-detail-heading`}>{selectedSkill.intent}</h3>
                <p className="record-id">{selectedSkill.id}</p>
              </header>

              <div className={styles.detailSections}>
                <section className={styles.detailBlock} aria-labelledby={`${id}-capabilities-heading`}>
                  <h4 id={`${id}-capabilities-heading`}>Capabilities</h4>
                  {selectedSkill.capabilities.length > 0
                    ? <ul className="tag-list">{selectedSkill.capabilities.map((item) => <li key={item}>{item}</li>)}</ul>
                    : <p>No capabilities listed by the gateway.</p>}
                </section>

                <section className={`${styles.detailBlock} ${styles.detailBlockWide}`} aria-labelledby={`${id}-sequence-heading`}>
                  <h4 id={`${id}-sequence-heading`}>Workflow sequence</h4>
                  {selectedSkill.sequence.length > 0
                    ? <ol className="sequence-list">{selectedSkill.sequence.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ol>
                    : <p>No sequence supplied by the gateway.</p>}
                </section>

                <section className={styles.detailBlock} aria-labelledby={`${id}-tools-heading`}>
                  <h4 id={`${id}-tools-heading`}>Supporting tools</h4>
                  {selectedSkill.supporting_tools && selectedSkill.supporting_tools.length > 0
                    ? <ul className="tag-list">{selectedSkill.supporting_tools.map((item) => <li key={item}>{item}</li>)}</ul>
                    : <p>No supporting tools listed by the gateway.</p>}
                </section>

                <section className={styles.detailBlock} aria-labelledby={`${id}-pitfalls-heading`}>
                  <h4 id={`${id}-pitfalls-heading`}>Known pitfalls</h4>
                  {selectedSkill.pitfalls.length > 0
                    ? <ul className="pitfall-list">{selectedSkill.pitfalls.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul>
                    : <p>No pitfalls listed by the gateway.</p>}
                </section>

                <MetadataDetails skill={selectedSkill} />
              </div>
            </section>
          </div>
        ) : null}
      </div>
    </section>
  );
}
