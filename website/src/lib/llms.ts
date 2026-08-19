import { getCollection } from 'astro:content';

export interface DocPage {
	/** Collection id, e.g. `guides/contexts` or `index`. */
	id: string;
	/** First path segment, or `''` for the site root page. */
	section: string;
	title: string;
	description: string;
	/** Raw Markdown/MDX source, frontmatter already stripped. */
	body: string;
}

/**
 * Human labels for the sidebar groups, keyed by directory name, in the
 * order the sidebar in `astro.config.mjs` lists them.  A directory absent
 * here still appears — under its own name, sorted last.
 */
const SECTION_LABELS: Record<string, string> = {
	'': 'Overview',
	'getting-started': 'Getting Started',
	guides: 'Guides',
	reference: 'Reference',
	deployment: 'Deployment',
};

/** Insertion order of the labels above; the two cannot drift apart. */
const SECTION_ORDER = Object.keys(SECTION_LABELS);

export function sectionLabel(section: string): string {
	return SECTION_LABELS[section] ?? section;
}

/**
 * Every published documentation page, ordered as the sidebar orders them.
 *
 * Drafts are dropped: a page the site does not publish is not a page the
 * index may point at.
 */
export async function docPages(): Promise<DocPage[]> {
	const entries = await getCollection('docs', ({ data }) => data.draft !== true);

	const pages: DocPage[] = entries.map((entry) => {
		const id = entry.id === '' ? 'index' : entry.id;
		const slashIndex = id.indexOf('/');
		return {
			id,
			section: slashIndex === -1 ? '' : id.slice(0, slashIndex),
			title: entry.data.title,
			description: entry.data.description ?? '',
			body: entry.body ?? '',
		};
	});

	pages.sort((a, b) => {
		const sectionDelta =
			sectionRank(a.section) - sectionRank(b.section);
		if (sectionDelta !== 0) return sectionDelta;
		return a.id.localeCompare(b.id);
	});
	return pages;
}

function sectionRank(section: string): number {
	const index = SECTION_ORDER.indexOf(section);
	return index === -1 ? SECTION_ORDER.length : index;
}

/** Where the Markdown source of *id* is served. */
export function pageMarkdownUrl(id: string, site: URL | string): URL {
	return new URL(`/llms/${id}.md`, site);
}

/**
 * The title-and-description block that precedes a page's body.
 *
 * Shared so the per-page route and the concatenated `/llms-full.txt`
 * cannot drift into two spellings of one format.
 */
export function pageHeader(page: DocPage): string {
	return page.description
		? `# ${page.title}\n\n> ${page.description}\n`
		: `# ${page.title}\n`;
}
