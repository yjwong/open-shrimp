import type { APIRoute } from 'astro';
import { docPages, pageMarkdownUrl, sectionLabel } from '../lib/llms';

export const prerender = true;

/**
 * `/llms.txt` — the index of every documentation page.
 *
 * One fetch has to be enough to reach the whole site, so every published
 * page appears here with the URL of its Markdown source.  A reader that
 * only ever sees this file never has to guess a URL.
 *
 * Format follows https://llmstxt.org/.
 */
export const GET: APIRoute = async (context) => {
	const site = context.site!;
	const pages = await docPages();

	const lines: string[] = [
		'# OpenShrimp',
		'',
		'> A Telegram bot for remote coding-agent access. It runs on your own',
		'> computer, drives the Claude Agent SDK or OpenCode, and asks before it',
		'> changes anything.',
		'',
		'Each link below points at the Markdown source of one page. Drop the',
		'`/llms/` prefix and the `.md` suffix to get the human-readable page',
		'(for example `/llms/guides/contexts.md` renders at `/guides/contexts/`).',
		'',
		`The whole site concatenated is at ${new URL('/llms-full.txt', site)}.`,
		'',
	];

	// `docPages()` sorts by section, so a heading is due whenever the
	// section changes — no intermediate grouping needed.
	let openSection: string | null = null;
	for (const page of pages) {
		if (page.section !== openSection) {
			if (openSection !== null) lines.push('');
			lines.push(`## ${sectionLabel(page.section)}`, '');
			openSection = page.section;
		}
		const url = pageMarkdownUrl(page.id, site);
		const suffix = page.description ? `: ${page.description}` : '';
		lines.push(`- [${page.title}](${url})${suffix}`);
	}
	lines.push('');

	return new Response(lines.join('\n'), {
		headers: { 'Content-Type': 'text/plain; charset=utf-8' },
	});
};
