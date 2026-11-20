// PDF bytes stay in the browser. Serve the parser locally for reliable demos.
import * as pdfjs from './pdf.min.mjs';
pdfjs.GlobalWorkerOptions.workerSrc = new URL('./pdf.worker.min.mjs', import.meta.url).href;

export async function pdfText(file) {
  const task = pdfjs.getDocument({data: await file.arrayBuffer()});
  try {
    const pdf = await task.promise;
    const pages = [];
    for (let page = 1; page <= pdf.numPages; page++) {
      const {items} = await (await pdf.getPage(page)).getTextContent();
      // Separate adjacent fragments so skills and dates do not merge into words.
      pages.push(items.map(item => item.str + (item.hasEOL ? '\n' : ' ')).join(''));
    }
    return pages.join('\n\n').trim();
  } finally {
    await task.destroy();
  }
}
