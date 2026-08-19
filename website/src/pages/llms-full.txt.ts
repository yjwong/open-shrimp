import type { APIRoute } from 'astro';
import { docPages, pageHeader } from '../lib/llms';

export const prerender = true;

/**
 * `/llms-full.txt` — every documentation page in one response.
 *
 * The escape hatch for a reader that would rather spend one fetch than
 * walk the index in `/llms.txt`.
 */
export const GET: APIRoute = async () => {
	const pages = await docPages();
	const parts = pages.map((page) => `${pageHeader(page)}\n${page.body.trim()}\n`);
	const body =
		'<SYSTEM>The complete OpenShrimp documentation.</SYSTEM>\n\n' +
		parts.join('\n---\n\n');
	return new Response(body, {
		headers: { 'Content-Type': 'text/plain; charset=utf-8' },
	});
};
