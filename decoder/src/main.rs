use std::io::{Read, Write};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let size: usize = std::env::args().nth(1).ok_or("missing output size")?.parse()?;
    let mut packed = Vec::new();
    std::io::stdin().read_to_end(&mut packed)?;
    let mut output = vec![0_u8; size];
    let count = oozextract::Extractor::new().read_from_slice(&packed, &mut output)?;
    if count != size {
        return Err(format!("decoded length {count} != {size}").into());
    }
    std::io::stdout().write_all(&output)?;
    Ok(())
}
